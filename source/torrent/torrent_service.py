import hashlib
import os
import queue
import threading
import time
import urllib.parse
from typing import List

import bencode
import requests
from RTN import parse

from jackett.jackett_result import JackettResult
from torrent.torrent_item import TorrentItem
from utils.general import get_info_hash_from_magnet
from utils.general import season_episode_in_filename
from utils.logger import setup_logger

class TorrentService:
    def __init__(self):
        self.logger = setup_logger(__name__)
        self.__session = requests.Session()

    def convert_and_process(self, results: List[JackettResult]):
        threads = []
        torrent_items_queue = queue.Queue()

        def thread_target(result: JackettResult):
            torrent_item = result.convert_to_torrent_item()

            if torrent_item.link.startswith("magnet:"):
                processed_torrent_item = self.__process_magnet(torrent_item)
            else:
                processed_torrent_item = self.__process_web_url(torrent_item)

            torrent_items_queue.put(processed_torrent_item)

        for result in results:
            threads.append(threading.Thread(target=thread_target, args=(result,)))

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        torrent_items_result = []

        while not torrent_items_queue.empty():
            torrent_items_result.append(torrent_items_queue.get())

        return torrent_items_result

    def __process_web_url(self, result: TorrentItem):
        self.logger.info("PROCESSING URL " + result.link)
        try:
            # TODO: is the default timeout enough?
            response = self.__session.get(result.link, allow_redirects=False, timeout=os.environ.get("JACKETT_RESOLVER_TIMEOUT", 2))
        except requests.exceptions.ReadTimeout:
            self.logger.error(f"Timeout while processing url (took longer than 2 seconds)")
            return result
        except requests.exceptions.RequestException:
            self.logger.error(f"Error while processing url: {result.link}")
            return result

        if response.status_code == 200:
            return self.__process_torrent(result, response.content)
        elif response.status_code == 302:
            result.magnet = response.headers['Location']
            return self.__process_magnet(result)
        else:
            self.logger.error(f"Error code {response.status_code} while processing url: {result.link}")

        return result

    def __process_torrent(self, result: TorrentItem, torrent_file):
        metadata = bencode.bdecode(torrent_file)

        result.torrent_download = result.link
        result.trackers = self.__get_trackers_from_torrent(metadata)
        result.info_hash = self.__convert_torrent_to_hash(metadata["info"])
        result.magnet = self.__build_magnet(result.info_hash, metadata["info"]["name"], result.trackers)

        if "files" not in metadata["info"]:
            result.file_index = 1
            return result

        result.files = metadata["info"]["files"]

        if result.type == "series":
            file_details = self.__find_episode_file(result.files, result.parsed_data.seasons, result.parsed_data.episodes)

            if file_details is not None:
                self.logger.info("File details")
                self.logger.info(file_details)
                result.file_index = file_details["file_index"]
                result.file_name = file_details["title"]
                result.size = file_details["size"]
        else:
            result.file_index = self.__find_movie_file(result.files)

        return result

    def __process_magnet(self, result: TorrentItem):
        if result.magnet is None:
            result.magnet = result.link

        if result.info_hash is None:
            result.info_hash = get_info_hash_from_magnet(result.magnet)

        result.trackers = self.__get_trackers_from_magnet(result.magnet)

        return result

    def __convert_torrent_to_hash(self, torrent_contents):
        hashcontents = bencode.bencode(torrent_contents)
        hexHash = hashlib.sha1(hashcontents).hexdigest()
        return hexHash.lower()

    def __build_magnet(self, hash, display_name, trackers):
        magnet_base = "magnet:?xt=urn:btih:"
        magnet = f"{magnet_base}{hash}&dn={display_name}"

        if len(trackers) > 0:
            magnet = f"{magnet}&tr={'&tr='.join(trackers)}"

        return magnet

    def __get_trackers_from_torrent(self, torrent_metadata):
        # Sometimes list, sometimes string
        announce = torrent_metadata["announce"] if "announce" in torrent_metadata else []
        # Sometimes 2D array, sometimes 1D array
        announce_list = torrent_metadata["announce-list"] if "announce-list" in torrent_metadata else []

        trackers = set()
        if isinstance(announce, str):
            trackers.add(announce)
        elif isinstance(announce, list):
            for tracker in announce:
                trackers.add(tracker)

        for announce_list_item in announce_list:
            if isinstance(announce_list_item, list):
                for tracker in announce_list_item:
                    trackers.add(tracker)
            if isinstance(announce_list_item, str):
                trackers.add(announce_list_item)

        return list(trackers)

    def __get_trackers_from_magnet(self, magnet: str):
        url_parts = urllib.parse.urlparse(magnet)
        query_parts = urllib.parse.parse_qs(url_parts.query)

        trackers = []
        if "tr" in query_parts:
            trackers = query_parts["tr"]

        return trackers

    def __find_episode_file(self, file_structure, season, episode):

        if len(season) == 0 or len(episode) == 0:
            return None

        file_index = 1
        strict_episode_files = []
        episode_files = []
        for files in file_structure:
            for file in files["path"]:

                parsed_file = parse(file)

                if season[0] in parsed_file.seasons and episode[0] in parsed_file.episodes:
                    episode_files.append({
                        "file_index": file_index,
                        "title": file,
                        "size": files["length"]
                    })

            # Doesn't that need to be indented?
            file_index += 1

        return max(episode_files, key=lambda file: file["size"])

    def __find_movie_file(self, file_structure):
        max_size = 0
        max_file_index = 1
        current_file_index = 1
        for files in file_structure:
            if files["length"] > max_size:
                max_file_index = current_file_index
                max_size = files["length"]
            current_file_index += 1

        return max_file_index
