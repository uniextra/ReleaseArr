import os
import time
import logging
from datetime import datetime, timezone, timedelta
import requests
import qbittorrentapi
from dotenv import load_dotenv
import schedule

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

SONARR_URL = os.getenv("SONARR_URL", "http://localhost:8989").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY")
QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://localhost:8080")
QBITTORRENT_USERNAME = os.getenv("QBITTORRENT_USERNAME", "admin")
QBITTORRENT_PASSWORD = os.getenv("QBITTORRENT_PASSWORD", "adminadmin")
DELAY_MINUTES = int(os.getenv("DELAY_MINUTES", "120"))
FAKE_EXTENSIONS = [ext.strip().lower() for ext in os.getenv("FAKE_EXTENSIONS", ".exe,.iso,.scr,.bat").split(",")]
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

class SonarrClient:
    def __init__(self, url, api_key):
        self.url = url
        self.headers = {"X-Api-Key": api_key}
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def get_series(self):
        url = f"{self.url}/api/v3/series"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def get_episodes(self, series_id):
        url = f"{self.url}/api/v3/episode"
        response = self.session.get(url, params={"seriesId": series_id})
        response.raise_for_status()
        return response.json()

    def update_episodes_monitor_status(self, episode_ids, monitored):
        if not episode_ids:
            return
        url = f"{self.url}/api/v3/episode/monitor"
        data = {
            "episodeIds": episode_ids,
            "monitored": monitored
        }
        response = self.session.put(url, json=data)
        response.raise_for_status()

    def search_episodes(self, episode_ids):
        if not episode_ids:
            return
        url = f"{self.url}/api/v3/command"
        data = {
            "name": "EpisodeSearch",
            "episodeIds": episode_ids
        }
        response = self.session.post(url, json=data)
        response.raise_for_status()

    def get_torrent_categories(self):
        url = f"{self.url}/api/v3/downloadclient"
        categories = set()
        response = self.session.get(url)
        response.raise_for_status()
        clients = response.json()
        for client in clients:
            if client.get("protocol") == "torrent" and client.get("enable"):
                for field in client.get("fields", []):
                    if field.get("name") in ("tvCategory", "category"):
                        val = field.get("value")
                        if val:
                            categories.add(val)
        return list(categories)

    def get_queue(self):
        url = f"{self.url}/api/v3/queue"
        response = self.session.get(url)
        response.raise_for_status()
        data = response.json()
        # Sonarr v3 usually returns a dict with 'records', older might return list
        if isinstance(data, dict) and "records" in data:
            return data["records"]
        return data

    def mark_download_failed(self, queue_id):
        url = f"{self.url}/api/v3/queue/{queue_id}"
        params = {
            "removeFromClient": "true",
            "blocklist": "true"
        }
        response = self.session.delete(url, params=params)
        response.raise_for_status()

class QbitClient:
    def __init__(self, host, username, password):
        self.client = qbittorrentapi.Client(host=host, username=username, password=password)
        try:
            self.client.auth_log_in()
        except qbittorrentapi.LoginFailed as e:
            logger.error(f"qBittorrent login failed: {e}")
        except Exception as e:
            logger.error(f"qBittorrent connection failed during login: {e}")

    def get_downloading_torrents(self):
        return self.client.torrents_info(status_filter="downloading")

    def get_torrent_files(self, torrent_hash):
        return self.client.torrents_files(torrent_hash=torrent_hash)

    def delete_torrent(self, torrent_hash):
        self.client.torrents_delete(delete_files=True, torrent_hashes=torrent_hash)

def process_sonarr(sonarr):
    logger.info("Checking Sonarr episodes...")
    try:
        all_series = sonarr.get_series()
    except Exception as e:
        logger.error(f"Failed to fetch series from Sonarr: {e}")
        return

    now = datetime.now(timezone.utc)
    delay_delta = timedelta(minutes=DELAY_MINUTES)
    
    episodes_to_unmonitor = []
    episodes_to_monitor = []

    for series in all_series:
        if not series.get("monitored"):
            continue

        series_id = series["id"]
        
        # Keep track of monitored seasons for this series
        monitored_seasons = {season["seasonNumber"] for season in series.get("seasons", []) if season.get("monitored")}

        try:
            episodes = sonarr.get_episodes(series_id)
        except Exception as e:
            logger.error(f"Failed to fetch episodes for series {series.get('title')}: {e}")
            continue

        for ep in episodes:
            season_num = ep.get("seasonNumber")
            
            # Skip unmonitored seasons entirely (except season 0 which might be specials)
            if season_num not in monitored_seasons:
                continue

            air_date_utc_str = ep.get("airDateUtc")
            if not air_date_utc_str:
                continue
                
            try:
                # airDateUtc is usually something like '2023-10-18T01:00:00Z'
                air_date_utc = datetime.fromisoformat(air_date_utc_str.replace('Z', '+00:00'))
            except ValueError:
                continue

            is_monitored = ep.get("monitored")
            has_file = ep.get("hasFile")
            
            # If it already has a file, we don't need to change its monitored state
            if has_file:
                continue

            available_time = air_date_utc + delay_delta
            
            # If episode hasn't reached its availability time yet
            if now < available_time:
                if is_monitored:
                    logger.info(f"Unmonitoring: {series['title']} - S{season_num:02d}E{ep['episodeNumber']:02d} (Airs: {air_date_utc})")
                    episodes_to_unmonitor.append(ep["id"])
            else:
                # If episode HAS reached availability time
                if not is_monitored:
                    logger.info(f"Monitoring: {series['title']} - S{season_num:02d}E{ep['episodeNumber']:02d} (Aired: {air_date_utc})")
                    episodes_to_monitor.append(ep["id"])

    if episodes_to_unmonitor:
        try:
            sonarr.update_episodes_monitor_status(episodes_to_unmonitor, False)
            logger.info(f"Successfully unmonitored {len(episodes_to_unmonitor)} episodes.")
        except Exception as e:
            logger.error(f"Failed to unmonitor episodes: {e}")

    if episodes_to_monitor:
        try:
            sonarr.update_episodes_monitor_status(episodes_to_monitor, True)
            logger.info(f"Successfully monitored {len(episodes_to_monitor)} episodes.")
            
            logger.info("Triggering search for newly monitored episodes...")
            sonarr.search_episodes(episodes_to_monitor)
        except Exception as e:
            logger.error(f"Failed to monitor or search episodes: {e}")

def process_qbittorrent(qbit, sonarr):
    logger.info("Checking qBittorrent for fake releases...")
    
    try:
        categories = sonarr.get_torrent_categories()
        if not categories:
            logger.warning("Could not find any torrent categories in Sonarr. Falling back to 'tv-sonarr'.")
            categories = ["tv-sonarr"]
        else:
            logger.info(f"Retrieved Sonarr categories: {categories}")
    except Exception as e:
        logger.error(f"Failed to fetch categories from Sonarr: {e}")
        categories = ["tv-sonarr"]

    # Map torrent hash to Sonarr queue ID and Episode ID
    queue_map = {}
    try:
        queue = sonarr.get_queue()
        for item in queue:
            download_id = item.get("downloadId")
            if download_id:
                queue_map[download_id.lower()] = {
                    "queue_id": item.get("id"),
                    "episode_id": item.get("episodeId")
                }
    except Exception as e:
        logger.error(f"Failed to fetch Sonarr queue: {e}")

    try:
        torrents = qbit.get_downloading_torrents()
    except Exception as e:
        logger.error(f"Failed to fetch torrents from qBittorrent: {e}")
        return

    for torrent in torrents:
        # Check if the torrent belongs to one of the Sonarr categories
        if torrent.category not in categories:
            continue

        try:
            files = qbit.get_torrent_files(torrent.hash)
        except Exception as e:
            logger.error(f"Failed to fetch files for torrent {torrent.name}: {e}")
            continue

        fake_found = False
        for f in files:
            name = f.name.lower()
            if any(name.endswith(ext) for ext in FAKE_EXTENSIONS):
                fake_found = True
                logger.warning(f"Fake file found in torrent '{torrent.name}' -> file: '{f.name}'")
                break
        
        if fake_found:
            t_hash = torrent.hash.lower()
            if t_hash in queue_map:
                logger.info(f"Torrent '{torrent.name}' found in Sonarr queue. Marking as failed and blocklisting...")
                q_item = queue_map[t_hash]
                try:
                    # Remove from Sonarr (which will remove from qBit and blocklist)
                    sonarr.mark_download_failed(q_item["queue_id"])
                    
                    # Trigger an explicit search again for the episode
                    if q_item.get("episode_id"):
                        logger.info("Triggering new search in Sonarr for the episode...")
                        sonarr.search_episodes([q_item["episode_id"]])
                except Exception as e:
                    logger.error(f"Failed to mark torrent as failed in Sonarr: {e}")
            else:
                logger.info(f"Torrent '{torrent.name}' not in Sonarr queue. Deleting manually from qBittorrent...")
                try:
                    qbit.delete_torrent(torrent.hash)
                except Exception as e:
                    logger.error(f"Failed to delete torrent {torrent.name}: {e}")

def job():
    if not SONARR_API_KEY:
        logger.error("SONARR_API_KEY is not set. Please check your .env file.")
        return

    sonarr = SonarrClient(SONARR_URL, SONARR_API_KEY)
    qbit = QbitClient(QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)
    
    process_sonarr(sonarr)
    process_qbittorrent(qbit, sonarr)

def main():
    logger.info("ReleaseArr script started.")
    logger.info(f"Check interval: {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Delay minutes after airtime: {DELAY_MINUTES}")
    logger.info(f"Fake extensions to look for: {FAKE_EXTENSIONS}")
    
    # Run once at startup
    job()
    
    # Schedule to run periodically
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job)
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
