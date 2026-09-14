import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Set, Optional
import requests
import qbittorrentapi
from dotenv import load_dotenv
import schedule

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

SONARR_URL = os.getenv("SONARR_URL", "http://localhost:8989").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://localhost:8080")
QBITTORRENT_USERNAME = os.getenv("QBITTORRENT_USERNAME", "admin")
QBITTORRENT_PASSWORD = os.getenv("QBITTORRENT_PASSWORD", "adminadmin")
DELAY_MINUTES = int(os.getenv("DELAY_MINUTES", "120"))
FAKE_EXTENSIONS = [ext.strip().lower() for ext in os.getenv("FAKE_EXTENSIONS", ".exe,.iso,.scr,.bat").split(",")]
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

class SonarrClient:
    def __init__(self, url: str, api_key: str):
        self.url: str = url
        self.headers: Dict[str, str] = {"X-Api-Key": api_key}
        self.session: requests.Session = requests.Session()
        self.session.headers.update(self.headers)

    def get_series(self) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/series"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def get_episodes(self, series_id: int) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/episode"
        response = self.session.get(url, params={"seriesId": series_id})
        response.raise_for_status()
        return response.json()

    def get_calendar(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/calendar"
        params = {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
            "unmonitored": "true"
        }
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def update_episodes_monitor_status(self, episode_ids: List[int], monitored: bool) -> None:
        if not episode_ids:
            return
        url = f"{self.url}/api/v3/episode/monitor"
        data = {
            "episodeIds": episode_ids,
            "monitored": monitored
        }
        response = self.session.put(url, json=data)
        response.raise_for_status()

    def search_episodes(self, episode_ids: List[int]) -> None:
        if not episode_ids:
            return
        url = f"{self.url}/api/v3/command"
        data = {
            "name": "EpisodeSearch",
            "episodeIds": episode_ids
        }
        response = self.session.post(url, json=data)
        response.raise_for_status()

    def get_torrent_categories(self) -> List[str]:
        url = f"{self.url}/api/v3/downloadclient"
        categories: Set[str] = set()
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

    def get_queue(self) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/queue"
        response = self.session.get(url)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "records" in data:
            return data["records"]
        return data

    def mark_download_failed(self, queue_id: int) -> None:
        url = f"{self.url}/api/v3/queue/{queue_id}"
        params = {
            "removeFromClient": "true",
            "blocklist": "true"
        }
        response = self.session.delete(url, params=params)
        response.raise_for_status()

class QbitClient:
    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.username = username
        self.password = password
        self.client = qbittorrentapi.Client(host=host, username=username, password=password)
        self.is_logged_in = False

    def login(self) -> bool:
        """Attempts to log in to qBittorrent. Returns True if successful."""
        try:
            self.client.auth_log_in()
            self.is_logged_in = True
            return True
        except Exception as e:
            logger.error(f"qBittorrent connection failed during login: {e}")
            self.is_logged_in = False
            return False

    def get_torrents(self) -> Any:
        return self.client.torrents_info()

    def get_torrent_files(self, torrent_hash: str) -> Any:
        return self.client.torrents_files(torrent_hash=torrent_hash)

    def delete_torrent(self, torrent_hash: str) -> None:
        self.client.torrents_delete(delete_files=True, torrent_hashes=torrent_hash)

def process_episodes(sonarr: SonarrClient, episodes: List[Dict[str, Any]], series_dict: Dict[int, Dict[str, Any]]) -> None:
    """Core logic to evaluate episodes and update their monitored status."""
    now = datetime.now(timezone.utc)
    delay_delta = timedelta(minutes=DELAY_MINUTES)
    
    episodes_to_unmonitor: List[int] = []
    episodes_to_monitor: List[int] = []

    for ep in episodes:
        series_id = ep.get("seriesId")
        series = series_dict.get(series_id)  # type: ignore
        
        # Skip if the series itself is not monitored or missing
        if not series or not series.get("monitored"):
            continue

        season_num = ep.get("seasonNumber")
        monitored_seasons = {season["seasonNumber"] for season in series.get("seasons", []) if season.get("monitored")}
        
        if season_num not in monitored_seasons:
            continue

        air_date_utc_str = ep.get("airDateUtc")
        if not air_date_utc_str:
            continue
            
        try:
            air_date_utc = datetime.fromisoformat(air_date_utc_str.replace('Z', '+00:00'))
        except ValueError:
            continue

        is_monitored = ep.get("monitored")
        has_file = ep.get("hasFile")
        
        if has_file:
            continue

        available_time = air_date_utc + delay_delta
        
        if now < available_time:
            if is_monitored:
                logger.info(f"Unmonitoring: {series['title']} - S{season_num:02d}E{ep['episodeNumber']:02d} (Airs: {air_date_utc})")
                episodes_to_unmonitor.append(ep["id"])
        else:
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

def process_sonarr_full(sonarr: SonarrClient) -> None:
    """Full scan: checks every episode of every series (Heavier on the API)."""
    logger.info("Performing FULL Sonarr scan (checking all series)...")
    try:
        all_series = sonarr.get_series()
    except Exception as e:
        logger.error(f"Failed to fetch series from Sonarr: {e}")
        return

    series_dict = {s["id"]: s for s in all_series}
    all_episodes = []

    for series in all_series:
        if not series.get("monitored"):
            continue
        try:
            eps = sonarr.get_episodes(series["id"])
            all_episodes.extend(eps)
        except Exception as e:
            logger.error(f"Failed to fetch episodes for series {series.get('title')}: {e}")
            continue

    process_episodes(sonarr, all_episodes, series_dict)

def process_sonarr_quick(sonarr: SonarrClient) -> None:
    """Quick scan: checks only the calendar for recent and upcoming episodes (Lighter on the API)."""
    logger.info("Performing QUICK Sonarr scan (Calendar)...")
    try:
        all_series = sonarr.get_series()
    except Exception as e:
        logger.error(f"Failed to fetch series from Sonarr: {e}")
        return

    series_dict = {s["id"]: s for s in all_series}

    now = datetime.now(timezone.utc)
    # Check episodes that aired in the last 7 days, up to 14 days in the future
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=14)

    try:
        episodes = sonarr.get_calendar(start_date, end_date)
    except Exception as e:
        logger.error(f"Failed to fetch calendar from Sonarr: {e}")
        return

    process_episodes(sonarr, episodes, series_dict)

def process_qbittorrent(qbit: QbitClient, sonarr: SonarrClient) -> None:
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

    queue_map: Dict[str, Dict[str, Any]] = {}
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
        torrents = qbit.get_torrents()
    except Exception as e:
        logger.error(f"Failed to fetch torrents from qBittorrent: {e}")
        return

    for torrent in torrents:
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
                    sonarr.mark_download_failed(q_item["queue_id"])
                    
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

def job(sonarr: SonarrClient, qbit: QbitClient, is_full_scan: bool = False) -> None:
    if is_full_scan:
        process_sonarr_full(sonarr)
    else:
        process_sonarr_quick(sonarr)
        
    # Only process qBittorrent if we can successfully establish a connection this cycle
    if qbit.login():
        process_qbittorrent(qbit, sonarr)
    else:
        logger.warning("Skipping qBittorrent checks this cycle due to connection issues.")

def main():
    if not SONARR_API_KEY:
        logger.error("SONARR_API_KEY is not set. Please check your .env file.")
        return

    logger.info("ReleaseArr script started.")
    logger.info(f"Check interval: {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Delay minutes after airtime: {DELAY_MINUTES}")
    logger.info(f"Fake extensions to look for: {FAKE_EXTENSIONS}")
    
    # Initialize clients ONCE
    sonarr = SonarrClient(SONARR_URL, SONARR_API_KEY)
    qbit = QbitClient(QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)
    
    # Run a full scan on startup to catch everything up
    job(sonarr, qbit, is_full_scan=True)
    
    # Schedule quick scans every X minutes (much lighter on the API)
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job, sonarr=sonarr, qbit=qbit, is_full_scan=False)
    
    # Schedule a full scan once a day to ensure older series aren't left behind if they get edited
    schedule.every().day.at("03:00").do(job, sonarr=sonarr, qbit=qbit, is_full_scan=True)
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
