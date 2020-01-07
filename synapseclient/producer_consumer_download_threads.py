import time
from queue import Queue
from threading import Thread, Lock
from typing import Generator, Sequence
from datetime import datetime
from math import ceil
from urllib.parse import urlparse, parse_qs
from urllib3.util.retry import Retry
from synapseclient.core.utils import printTransferProgress
from synapseclient.core.exceptions import SynapseError
from requests import Session, Response
from requests.adapters import HTTPAdapter
from os.path import basename

# constants
BUF_SIZE = 20
MAX_RETRIES = 5
MB = 2**20
SYNAPSE_DEFAULT_DOWNLOAD_PART_SIZE = 8 * MB
ISO_AWS_STR_FORMAT = '%Y%m%dT%H%M%SZ'
PARTIAL_CONTENT_CODE = 206
CONNECT_FACTOR = 3
BACK_OFF_FACTOR = 0.5
TIME_BUFFER = 2  # offset parameter used to buffer url expiration checks, time in seconds

# transfer progress parameters
transferred = 0
t0 = 0
to_be_transferred = 0
lock = Lock()

# Sentinel object to signal producer queue is done producing
SENTINEL = object()


class DownloadRequest:
    """
    A request to download a file from Synapse

    ...

    Attributes
    ----------
    file_handle_id : int
        The file handle ID to download.
    object_id : str
        The Synapse object this file associated to.
    object_type : str
        the type of the associated Synapse object.
    path : str
        The local path to download the file to.
        This path can be either absolute path or relative path from where the code is executed to the download location.
    """

    def __init__(self, file_handle_id: int, object_id: str, object_type: str, path: str):
        """

        :param file_handle_id:
        :param object_id:
        :param object_type:
        :param path:
        """

        self.file_handle_id = file_handle_id
        self.object_id = object_id
        self.object_type = object_type
        self.path = path


class CloseableQueue(Queue):
    """
    A closeable queue used to signal when producers are finished producing so consumer threads know when to terminate.
    Adopted from Effective Python Item 39.
    """

    def close(self):
        self.put(SENTINEL)

    def __iter__(self):
        while True:
            item = self.get()
            try:
                if item is SENTINEL:
                    return  # cause the thread to exit
                yield item
            finally:
                self.task_done()


class ProducerDownloadThread(Thread):
    """
    The producer threads that make the GET request and obtain the data for a download chunk
    """
    def __init__(self, client, request, range_queue, data_queue):
        Thread.__init__(self)
        self.setDaemon(True)
        self.client = client
        self.request = request
        self.range_queue = range_queue
        self.data_queue = data_queue
        self.session = _get_new_session()

    def run(self):
        for item in self.range_queue:
            start, end, file_name, url, path = item
            headers = {'Range': 'bytes=%d-%d' % (start, end)}
            response = _get_response_with_refresh(url, self.client, self.request, headers, self.session)

            # try request until successful or out of retries
            try_counter = 0
            while response.status_code != PARTIAL_CONTENT_CODE and try_counter < MAX_RETRIES:
                try_counter += 1
                response = _get_response_with_refresh(url, self.client, self.request, headers, self.session)

            if response.status_code == PARTIAL_CONTENT_CODE:
                self.data_queue.put((start, file_name, path, response.content))
            else:
                raise SynapseError("Could not download the file: %s, please try again." % file_name)


class ConsumerDownloadThread(Thread):
    """
    The worker threads that write download chunks to file
    """
    def __init__(self, data_queue):
        Thread.__init__(self)
        self.setDaemon(True)
        self.data_queue = data_queue

    def run(self):
        for item in self.data_queue:
            start, file_name, path, data = item
            # write data to file
            with open(path, "r+b") as file_write:
                file_write.seek(start)
                file_write.write(data)
            global transferred
            with lock:
                transferred += len(data)
            printTransferProgress(transferred, to_be_transferred, 'Downloading ', basename(path), dt=time.time()-t0)


def download_files(client,
                   download_requests: Sequence[DownloadRequest],
                   num_threads: int):
    """
    Main driver for the multi-threaded download. Uses the producer-consumer with Queue design pattern as described
    in Effective Python Item 39.

    :param client: A synapseclient
    :param download_requests: A batch of DownloadRequest objects specifying what Synapse files to download
    :param num_threads: The number of download threads
    :return: Map between each DownloadRequest in download_requests object and the corresponding DownloadResponse object
    """

    data_queue = CloseableQueue(BUF_SIZE)
    range_queue = CloseableQueue(BUF_SIZE)

    for request in download_requests:
        producer_threads = []
        consumer_threads = []
        for _ in range(num_threads):
            producer_worker = ProducerDownloadThread(client, request, range_queue, data_queue)
            consumer_worker = ConsumerDownloadThread(data_queue)
            producer_threads.append(producer_worker)
            consumer_threads.append(consumer_worker)

        for producer_thread, consumer_thread in zip(producer_threads, consumer_threads):
            producer_thread.start()
            consumer_thread.start()

        file_name, pre_signed_url = _get_pre_signed_batch_request_json(client, request)
        file_size = _get_file_size(pre_signed_url)
        pre_signed_url_chunk_generator = _get_chunk_pre_signed_url(file_size,
                                                                   file_name,
                                                                   pre_signed_url,
                                                                   request.path)
        _create_empty_file(file_size, request.path)

        global to_be_transferred
        to_be_transferred = file_size

        global transferred
        transferred = 0

        global t0
        t0 = time.time()

        for chunk in pre_signed_url_chunk_generator:
            range_queue.put(chunk)
        range_queue.close()

    range_queue.join()
    data_queue.close()
    data_queue.join()


def _get_pre_signed_batch_request_json(client, request: DownloadRequest) -> tuple:
    """
    Returns the file_name and pre-signed url for download as specified in request

    :param client: The synapseclient being used for download
    :param request: An individual entry in the form of a DownloadRequest
    :return: A tuple containing the file_name and pre-signed url
    """
    # noinspection PyProtectedMember
    response = client._getFileHandleDownload(request.file_handle_id,  request.object_id)
    file_name = response["fileHandle"]["fileName"]
    pre_signed_url = response["preSignedURL"]
    return file_name, pre_signed_url


def _get_chunk_pre_signed_url(file_size: int,
                              file_name: str,
                              url: str,
                              path: str
                              ) -> Generator:
    """
    Creates a generator which yields byte ranges and meta data required to make a range request download of url and
    write the data to file_name located at path. Download chunk sizes are 8MB by default.

    :param file_size: The size of the file
    :param file_name: The name of the file
    :param url: The pre-signed url to download the file from
    :param path: The local path describing where to download the file
    :return: A generator of byte ranges and meta data needed to download the file in a multi-threaded manner
    """
    num_chunks = ceil(file_size / SYNAPSE_DEFAULT_DOWNLOAD_PART_SIZE)
    for i in range(num_chunks):
        start = SYNAPSE_DEFAULT_DOWNLOAD_PART_SIZE * i
        end = start + SYNAPSE_DEFAULT_DOWNLOAD_PART_SIZE
        yield start, end, file_name, url, path


def _url_is_valid(url: str) -> bool:
    """
    Checks if url is expired

    :param url: A pre-signed download url from AWS
    :return: True if url is not expired (i.e. valid), False otherwise
    """
    parsed_url = urlparse(url)
    time_made = parse_qs(parsed_url.query)['X-Amz-Date'][0]
    time_made_datetime = datetime.strptime(time_made, ISO_AWS_STR_FORMAT)
    expires = parse_qs(parsed_url.query)['X-Amz-Expires'][0]
    time_delta_seconds = (datetime.utcnow() - time_made_datetime).total_seconds()
    return time_delta_seconds + TIME_BUFFER < int(expires)


def _create_empty_file(file_size, path) -> None:
    """
    Creates an empty file named file_name at location path and of size file_size

    :param file_size: The size of the file (in Bytes)
    :param path: The local path describing where to download the file
    :return: None
    """
    with open(path, "wb") as file:
        file.seek(file_size - 1)
        file.write(b'\0')


def _get_new_session() -> Session:
    """
    Creates a new requests.Session object with retry defined by CONNECT_FACTOR and BACK_OFF_FACTOR
    :return: A new requests.Session object
    """
    session = Session()
    retry = Retry(connect=CONNECT_FACTOR, backoff_factor=BACK_OFF_FACTOR)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def _get_file_size(url: str) -> int:
    """
    Gets the size of the file located at url
    :param url: The pre-signed url of the file
    :return: The size of the file in bytes
    """
    session = _get_new_session()
    res_get = session.get(url, stream=True)
    return int(res_get.headers['Content-Length'])


def _refresh_url_if_expired(url: str, client, request: DownloadRequest) -> str:
    """
    Checks if url is expired, if so returns refresh pre-signed url
    :param url: A pre-signed url to be checked and possibly refreshed
    :param client: The synapseclient being used to download
    :param request: The DownloadRequest specifying the file located at url
    :param return: A pre-signed url for the file defined in request
    """
    if not _url_is_valid(url):
        _, pre_signed_url_new = _get_pre_signed_batch_request_json(client, request)
        return pre_signed_url_new
    return url


def _get_response_with_refresh(url: str, client, request: DownloadRequest,
                               headers: dict, session: Session) -> Response:
    """
    Performs refresh on url if necessary and returns response for range request on url specified by headers
    :param url: A pre-signed url pointing to file to download
    :param client: The synapseclient being used to download
    :param request: The DownloadRequest specifying the file located at url
    :param headers: A dict specifying the byte range for the range request of url
    :param session: The current request.Session object to make the get call with
    :return: The requests.Response from calling get on url
    """
    url = _refresh_url_if_expired(url, client, request)
    return session.get(url, headers=headers, stream=True)