"""Background work: fetching link details, covers, downloads, retries, checks, installs."""

from gui.workers.batch_worker import BatchWorker
from gui.workers.dependency_worker import DependencyWorker
from gui.workers.download_queue import DownloadQueue, QueueItem
from gui.workers.download_worker import DownloadWorker, output_template
from gui.workers.ffmpeg_installer import FFmpegInstallWorker
from gui.workers.image_worker import ImageWorker
from gui.workers.log_bridge import LogBridge
from gui.workers.metadata_worker import MetadataWorker, url_kind
from gui.workers.retry_worker import RetryWorker
from gui.workers.spotdl_updater import SpotdlUpdateWorker

YtdlpUpdateWorker = SpotdlUpdateWorker      # older name

__all__ = ["BatchWorker", "DependencyWorker", "DownloadQueue", "QueueItem", "DownloadWorker",
           "FFmpegInstallWorker", "ImageWorker", "LogBridge", "MetadataWorker", "RetryWorker",
           "SpotdlUpdateWorker", "YtdlpUpdateWorker", "output_template", "url_kind"]
