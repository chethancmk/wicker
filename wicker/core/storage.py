"""
This file is for S3 storage routines used by S3 datasets

Boto3 S3 documentation links:
- https://boto3.amazonaws.com/v1/documentation/api/latest/guide/index.html
- https://boto3.amazonaws.com/v1/documentation/api/latest/guide/resources.html
- https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html
"""
import io
import logging
import os
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import boto3
import boto3.session
import botocore  # type: ignore
from botocore.exceptions import ClientError  # type: ignore
from retry import retry

##Changes for GCS
from google.cloud import storage


from wicker.core.config import get_config
from wicker.core.definitions import DatasetID, DatasetPartition
from wicker.core.filelock import SimpleUnixFileLock
from wicker.core.utils import time_limit

logger = logging.getLogger(__name__)


class S3DataStorage:
    """Storage routines for reading and writing objects in S3"""

    def __init__(self, session: Optional[boto3.session.Session] = None) -> None:
        """Constructor for an S3Storage instance

        Part of the reason to structure S3 routines as a class rather utility functions is to enable
        unit tests to easily mock / patch the S3 client or to utilize the S3 Stubber. Unit tests
        might also find it convenient to mock or patch member functions on instances of this class.
        """
        boto_config = get_config().aws_s3_config.boto_config
        boto_client_config = botocore.config.Config(
            max_pool_connections=boto_config.max_pool_connections,
            read_timeout=boto_config.read_timeout_s,
            connect_timeout=boto_config.connect_timeout_s,
        )
        self.session = boto3.session.Session() if session is None else session
        self.client = self.session.client("s3", config=boto_client_config)
        self.read_timeout = get_config().storage_download_config.timeout

    def __getstate__(self) -> Dict[Any, Any]:
        return {}

    def __setstate__(self, state: Dict[Any, Any]) -> None:
        self.session = boto3.session.Session()
        self.client = self.session.client("s3")
        self.read_timeout = get_config().storage_download_config.timeout
        return

    @staticmethod
    def bucket_key_from_s3_path(s3_path: str) -> Tuple[str, str]:
        """Parses a fully-qualified S3 path such as "s3://hello/world" into the bucket and key

        :param s3_path: fully-qualified S3 path
        :type s3_path: str
        :return: tuple consisting of the S3 bucket and key
        :rtype: Tuple[str, str]
        """
        url_parsed = urlparse(s3_path)
        return url_parsed.netloc, url_parsed.path[1:]

    def check_exists_s3(self, input_path: str) -> bool:
        """Checks if a file exists on S3 under given path

        :param input_path: input file path in S3
        :type input_path: str
        :return: whether or not the file exists in S3
        :rtype: bool
        """
        bucket, key = self.bucket_key_from_s3_path(input_path)
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False

    @retry(
        Exception,
        tries=get_config().storage_download_config.retries,
        backoff=get_config().storage_download_config.retry_backoff,
        delay=get_config().storage_download_config.retry_delay_s,
        logger=logger,
    )
    def download_with_retries(self, bucket: str, key: str, local_path: str):
        try:
            with time_limit(
                self.read_timeout, f"Timing out in trying to download object for bucket: {bucket}, key: {key}"
            ):
                self.client.download_file(bucket, key, local_path)
        except Exception as e:
            logging.error(f"Failed to download s3 object in bucket: {bucket}, key: {key}")
            raise e

    def fetch_file_s3(self, input_path: str, local_prefix: str, timeout_seconds: int = 120) -> str:
        """Fetches a file from S3 to the local machine and skips it if it already exists. This function
        is safe to call concurrently from multiple processes and utilizes a local filelock to block
        parallel downloads such that only one process will perform the download.

        This function assumes the input_path is a valid file in S3.

        :param input_path: input file path in S3
        :param local_prefix: local path that specifies where to download the file
        :param timeout_seconds: number of seconds till timing out on waiting for the file to be downloaded
        :return: local path to the downloaded file
        """
        bucket, key = self.bucket_key_from_s3_path(input_path)
        local_path = os.path.join(local_prefix, key)

        lock_path = local_path + ".lock"
        success_marker = local_path + ".success"
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        while not os.path.isfile(success_marker):
            with SimpleUnixFileLock(lock_path, timeout_seconds=timeout_seconds):
                if not os.path.isfile(success_marker):

                    # For now, we will only download the file if it has not already been downloaded already.
                    # Long term, we would also add a size check or md5sum comparison against the object in S3.
                    filedir = os.path.split(local_path)[0]
                    os.makedirs(filedir, exist_ok=True)
                    self.download_with_retries(bucket=bucket, key=key, local_path=local_path)
                    with open(success_marker, "w"):
                        pass

        return local_path

    def fetch_obj_s3(self, input_path: str) -> bytes:
        """Fetches an object from S3 as bytes in memory

        :param input_path: path to object in s3
        :return: bytes of data in file
        """
        bucket, key = self.bucket_key_from_s3_path(input_path)
        bio = io.BytesIO()
        self.client.download_fileobj(bucket, key, bio)
        return bio.getvalue()

    def put_object_s3(self, object_bytes: bytes, s3_path: str) -> None:
        """Upload an object to S3

        :param object_bytes: the object to upload to S3
        :type object_bytes: bytes
        :param s3_path: path to the file in S3
        :type s3_path: str
        """
        # Long term, we would add an md5sum check and short-circuit the upload if they are the same
        bucket, key = self.bucket_key_from_s3_path(s3_path)
        if s3_path.startswith('s3://'):
            self.client.put_object(Body=object_bytes, Bucket=bucket, Key=key)
        if s3_path.startswith('gs://'):
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket)
            blob = bucket.blob(key)
            blob.upload_from_string(object_bytes)

    def put_file_s3(self, local_path: str, s3_path: str) -> None:
        """Upload a file to S3

        :param local_path: local path to the file
        :param s3_path: s3 path to dump file to
        """
        bucket, key = self.bucket_key_from_s3_path(s3_path)
        if s3_path.startswith('s3://'):
            self.client.upload_file(local_path, bucket, key)
        if s3_path.startswith('gs://'):
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket)
            blob = bucket.blob(key)
            blob.upload_from_filename(filename=local_path)

    def __eq__(self, other: Any) -> bool:
        # We don't want to use isinstance here to make sure we have the same implementation.
        return super().__eq__(other) and type(self) == type(other)


class S3PathFactory:
    """Factory class for S3 paths

    Our bucket should look like::

        s3://<root_path>
        /__COLUMN_CONCATENATED_FILES__
            - file1
            - file2
        /dataset_name_1
        /dataset_name_2
            /0.0.1
            /0.0.2
            - avro_schema.json
            / assets
                - files added by users
            / partition_1.parquet
            / partition_2.parquet
                - _l5ml_dataset_partition_metadata.json
                - _SUCCESS
                - part-0-attempt-1234.parquet
                - part-1-attempt-2345.parquet

    If store_concatenated_bytes_files_in_dataset is True, then the bucket structure will
    be under the dataset directory:

            s3://<root_path>
            /dataset_name_1
            /dataset_name_2
                /__COLUMN_CONCATENATED_FILES__
                    - file1
                    - file2
                /0.0.1
                /0.0.2
                - avro_schema.json
                / assets
                    - files added by users
                / partition_1.parquet
                / partition_2.parquet
                    - _l5ml_dataset_partition_metadata.json
                    - _SUCCESS
                    - part-0-attempt-1234.parquet
                    - part-1-attempt-2345.parquet
    """

    def __init__(self, s3_root_path: str = None) -> None:
        s3_config = get_config().aws_s3_config
        if s3_root_path is None:
            s3_root_path = s3_config.s3_datasets_path
        self.root_path = s3_root_path
        self.store_concatenated_bytes_files_in_dataset = s3_config.store_concatenated_bytes_files_in_dataset

    def __eq__(self, other: Any) -> bool:
        return super().__eq__(other) and type(self) == type(other) and self.root_path == other.root_path

    def get_dataset_assets_path(self, dataset_id: DatasetID, s3_prefix: bool = True) -> str:
        full_path = os.path.join(
            self.root_path,
            dataset_id.name,
            dataset_id.version,
            "assets",
        )
        if not s3_prefix:
            return full_path.replace("s3://", "")
        return full_path

    def get_dataset_partition_metadata_path(self, data_partition: DatasetPartition, s3_prefix: bool = True) -> str:
        full_path = os.path.join(
            self.get_dataset_partition_path(data_partition),
            "_l5ml_dataset_partition_metadata.json",
        )
        if not s3_prefix:
            return full_path.replace("s3://", "")
        return full_path

    def get_dataset_partition_path(self, data_partition: DatasetPartition, s3_prefix: bool = True) -> str:
        full_path = os.path.join(
            self.root_path,
            data_partition.dataset_id.name,
            data_partition.dataset_id.version,
            f"{data_partition.partition}.parquet",
        )
        if not s3_prefix:
            return full_path.replace("s3://", "")
        return full_path

    def get_dataset_schema_path(self, dataset_id: DatasetID, s3_prefix: bool = True) -> str:
        full_path = os.path.join(
            self.root_path,
            dataset_id.name,
            dataset_id.version,
            "avro_schema.json",
        )
        if not s3_prefix:
            return full_path.replace("s3://", "")
        return full_path

    def get_column_concatenated_bytes_files_path(self, s3_prefix: bool = True, dataset_name: str = None) -> str:
        """Gets the path to the root of all column_concatenated_bytes files

        :param s3_prefix: whether to return the s3:// prefix, defaults to True
        :param dataset_name: if self.store_concatenated_bytes_files_in_dataset is True,
            it requires dataset name, defaults to None
        :return: path to the column_concatenated_bytes file with the file_id
        """
        if self.store_concatenated_bytes_files_in_dataset:
            if dataset_name is None:
                raise ValueError("Must provide dataset_id if store_concatenated_bytes_files_in_dataset is True")
            full_path = os.path.join(self.root_path, dataset_name, "__COLUMN_CONCATENATED_FILES__")
        else:
            full_path = os.path.join(self.root_path, "__COLUMN_CONCATENATED_FILES__")
        if not s3_prefix:
            return full_path.replace("s3://", "")
        return full_path

    def get_column_concatenated_bytes_s3path_from_uuid(self, file_uuid: bytes, dataset_name: str = None) -> str:
        columns_root_path = self.get_column_concatenated_bytes_files_path(dataset_name=dataset_name)
        return os.path.join(columns_root_path, str(uuid.UUID(bytes=file_uuid)))

    def get_temporary_row_files_path(self, dataset_id: DatasetID) -> str:
        full_path = os.path.join(
            self.root_path,
            "__TEMP_FILES__",
            dataset_id.name,
            dataset_id.version,
        )
        return full_path
