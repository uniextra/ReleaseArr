# ReleaseArr

ReleaseArr is a python script designed to integrate closely with Sonarr and qBittorrent to prevent fake releases (like `.exe` or `.iso` malware disguised as episodes) and prevent downloading episodes before they have actually aired.

Inspired by [swurApp](https://github.com/OwlCaribou/swurApp), ReleaseArr adds dynamic qBittorrent fake-file monitoring and uses Sonarr's internal APIs to properly blocklist fake torrents and trigger new searches automatically.

## Features
- **Air Date Waiter**: Automatically unmonitors episodes that haven't aired yet, and re-monitors them (triggering a search) after a configurable delay (e.g. 2 hours after the air time).
- **Fake Release Protection**: Monitors qBittorrent for actively downloading torrents. If a configured fake extension (e.g., `.exe`, `.scr`, `.bat`) is found, it:
  - Maps the torrent hash back to the Sonarr queue.
  - Commands Sonarr to mark the download as failed and **blocklists** the release.
  - Automatically triggers a new search in Sonarr to find a proper release.
- **Dynamic Sonarr Categories**: Automatically queries Sonarr for your configured Download Client categories (e.g., `tv-sonarr`), so it only monitors the torrents Sonarr actually sends to qBittorrent.

## Configuration

Configuration is handled via environment variables (or a `.env` file).

| Variable | Description | Default |
|----------|-------------|---------|
| `SONARR_URL` | The URL to your Sonarr instance | `http://localhost:8989` |
| `SONARR_API_KEY` | Your Sonarr API Key | |
| `QBITTORRENT_URL` | The URL to your qBittorrent Web UI | `http://localhost:8080` |
| `QBITTORRENT_USERNAME` | Your qBittorrent username | `admin` |
| `QBITTORRENT_PASSWORD` | Your qBittorrent password | `adminadmin` |
| `DELAY_MINUTES` | Minutes to wait after an episode's airtime before monitoring/searching for it | `120` |
| `FAKE_EXTENSIONS` | Comma-separated list of extensions to blocklist | `.exe,.iso,.scr,.bat,.cmd,.zip,.rar` |
| `CHECK_INTERVAL_MINUTES` | How often the script runs its checks | `10` |

## Installation & Usage

### Docker / Docker Compose

A Docker image is automatically built and published to Docker Hub.

```yaml
version: '3'
services:
  releasearr:
    image: uniextra/releasearr:latest
    container_name: releasearr
    restart: unless-stopped
    environment:
      - SONARR_URL=http://sonarr:8989
      - SONARR_API_KEY=your_api_key_here
      - QBITTORRENT_URL=http://qbittorrent:8080
      - QBITTORRENT_USERNAME=admin
      - QBITTORRENT_PASSWORD=adminadmin
      - DELAY_MINUTES=120
      - CHECK_INTERVAL_MINUTES=10
```

### Python / Bare Metal

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your details.
4. Run: `python release_arr.py`
