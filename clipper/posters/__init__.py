"""Platform connectors. Every poster has the same interface: configured(), can_post(), upload(), get_stats()."""
from .base import NotConnected, PostError, Poster, PostResult, get_connection, save_connection
from .instagram import InstagramPoster
from .tiktok import TikTokPoster
from .youtube import YouTubePoster

POSTERS = {"youtube": YouTubePoster, "tiktok": TikTokPoster, "instagram": InstagramPoster}
# Facebook Reels, X and Snapchat Spotlight plug in here as further Poster subclasses.


def get_poster(platform: str, cfg, conn) -> Poster:
    return POSTERS[platform](cfg, conn)
