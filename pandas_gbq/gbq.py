# Copyright (c) 2017 pandas-gbq Authors All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import copy
from datetime import datetime
import logging
import re
import time
import typing
from typing import Any, Dict, Optional, Sequence, Union
import warnings

import numpy as np

# Only import at module-level at type checking time to avoid circular
# dependencies in the pandas package, which has an optional dependency on
# pandas-gbq.
if typing.TYPE_CHECKING:  # pragma: NO COVER
    import pandas

import pandas_gbq.constants
import pandas_gbq.exceptions
from pandas_gbq.exceptions import GenericGBQException, QueryTimeout
from pandas_gbq.features import FEATURES
import pandas_gbq.query
import pandas_gbq.schema
import pandas_gbq.schema.pandas_to_bigquery
import pandas_gbq.timestamp

try:
    import tqdm  # noqa
except ImportError:
    tqdm = None

logger = logging.getLogger(__name__)


def _test_google_api_imports():
    try:
        import packaging  # noqa
    except ImportError as ex:  # pragma: NO COVER
        raise ImportError("pandas-gbq requires db-dtypes") from ex

    try:
        import db_dtypes  # noqa
    except ImportError as ex:  # pragma: NO COVER
        raise ImportError("pandas-gbq requires db-dtypes") from ex

    try:
        import pydata_google_auth  # noqa
    except ImportError as ex:  # pragma: NO COVER
        raise ImportError("pandas-gbq requires pydata-google-auth") from ex

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa
    except ImportError as ex:  # pragma: NO COVER
        raise ImportError("pandas-gbq requires google-auth-oauthlib") from ex

    try:
        import google.auth  # noqa
    except ImportError as ex:  # pragma: NO COVER
        raise ImportError("pandas-gbq requires google-auth") from ex

    try:
        from google.cloud import bigquery  # noqa
    except ImportError as ex:  # pragma: NO COVER
        raise ImportError("pandas-gbq requires google-cloud-bigquery") from ex


def _is_query(query_or_table: str) -> bool:
    return re.search(r"\s", query_or_table.strip(), re.MULTILINE) is not None


class DatasetCreationError(ValueError):
    """
    Raised when the create dataset method fails
    """


class InvalidColumnOrder(ValueError):
    """
    Raised when the provided column order for output
    results DataFrame does not match the schema
    returned by BigQuery.
    """


class InvalidIndexColumn(ValueError):
    """
    Raised when the provided index column for output
    results DataFrame does not match the schema
    returned by BigQuery.
    """


class InvalidPageToken(ValueError):
    """
    Raised when Google BigQuery fails to return,
    or returns a duplicate page token.
    """


class InvalidSchema(ValueError):
    """
    Raised when the provided DataFrame does
    not match the schema of the destination
    table in BigQuery.
    """

    def __init__(self, message: str):
        self._message = message

    @property
    def message(self) -> str:
        return self._message


class NotFoundException(ValueError):
    """
    Raised when the project_id, table or dataset provided in the query could
    not be found.
    """


class TableCreationError(ValueError):
    """
    Raised when the create table method fails
    """

    def __init__(self, message: str):
        self._message = message

    @property
    def message(self) -> str:
        return self._message


class Context(object):
    """Storage for objects to be used throughout a session.

    A Context object is initialized when the ``pandas_gbq`` module is
    imported, and can be found at :attr:`pandas_gbq.context`.
    """

    def __init__(self):
        self._credentials = None
        self._project = None
        # dialect defaults to None so that read_gbq can stop warning if set.
        self._dialect = None

    @property
    def credentials(self):
        """
        Credentials to use for Google APIs.

        These credentials are automatically cached in memory by calls to
        :func:`pandas_gbq.read_gbq` and :func:`pandas_gbq.to_gbq`. To
        manually set the credentials, construct an
        :class:`google.auth.credentials.Credentials` object and set it as
        the context credentials as demonstrated in the example below. See
        `auth docs`_ for more information on obtaining credentials.

        .. _auth docs: http://google-auth.readthedocs.io
            /en/latest/user-guide.html#obtaining-credentials

        Returns
        -------
        google.auth.credentials.Credentials

        Examples
        --------

        Manually setting the context credentials:

        >>> import pandas_gbq
        >>> from google.oauth2 import service_account
        >>> credentials = service_account.Credentials.from_service_account_file(
        ...     '/path/to/key.json',
        ... )
        >>> pandas_gbq.context.credentials = credentials
        """
        return self._credentials

    @credentials.setter
    def credentials(self, value):
        self._credentials = value

    @property
    def project(self):
        """Default project to use for calls to Google APIs.

        Returns
        -------
        str

        Examples
        --------

        Manually setting the context project:

        >>> import pandas_gbq
        >>> pandas_gbq.context.project = 'my-project'
        """
        return self._project

    @project.setter
    def project(self, value):
        self._project = value

    @property
    def dialect(self):
        """
        Default dialect to use in :func:`pandas_gbq.read_gbq`.

        Allowed values for the BigQuery SQL syntax dialect:

        ``'legacy'``
            Use BigQuery's legacy SQL dialect. For more information see
            `BigQuery Legacy SQL Reference
            <https://cloud.google.com/bigquery/docs/reference/legacy-sql>`__.
        ``'standard'``
            Use BigQuery's standard SQL, which is
            compliant with the SQL 2011 standard. For more information
            see `BigQuery Standard SQL Reference
            <https://cloud.google.com/bigquery/docs/reference/standard-sql/>`__.

        Returns
        -------
        str

        Examples
        --------

        Setting the default syntax to standard:

        >>> import pandas_gbq
        >>> pandas_gbq.context.dialect = 'standard'
        """
        return self._dialect

    @dialect.setter
    def dialect(self, value):
        self._dialect = value


# Create an empty context, used to cache credentials.
context = Context()
"""A :class:`pandas_gbq.Context` object used to cache credentials.

Credentials automatically are cached in-memory by :func:`pandas_gbq.read_gbq`
and :func:`pandas_gbq.to_gbq`.
"""


class GbqConnector(object):
    def __init__(
        self,
        project_id,
        reauth=False,
        private_key=None,
        auth_local_webserver=True,
        dialect="standard",
        location=None,
        credentials=None,
        use_bqstorage_api=False,
        auth_redirect_uri=None,
        client_id=None,
        client_secret=None,
        user_agent=None,
        rfc9110_delimiter=False,
        bigquery_client=None,
    ):
        global context
        from google.api_core.exceptions import ClientError, GoogleAPIError

        from pandas_gbq import auth

        self.http_error = (ClientError, GoogleAPIError)
        self.project_id = project_id
        self.location = location
        self.reauth = reauth
        self.private_key = private_key
        self.auth_local_webserver = auth_local_webserver
        self.dialect = dialect
        self.credentials = credentials
        self.auth_redirect_uri = auth_redirect_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.rfc9110_delimiter = rfc9110_delimiter
        self.use_bqstorage_api = use_bqstorage_api

        if bigquery_client is not None:
            # If a bq client is already provided, use it to populate auth fields.
            self.project_id = bigquery_client.project
            self.credentials = bigquery_client._credentials
            self.client = bigquery_client
            return

        default_project = None

        # Service account credentials have a project associated with them.
        # Prefer that project if none was supplied.
        if self.project_id is None and hasattr(self.credentials, "project_id"):
            self.project_id = credentials.project_id

        # Load credentials from cache.
        if not self.credentials:
            self.credentials = context.credentials
            default_project = context.project

        # Credentials were explicitly asked for, so don't use the cache.
        if private_key or reauth or not self.credentials:
            self.credentials, default_project = auth.get_credentials(
                private_key=private_key,
                project_id=project_id,
                reauth=reauth,
                auth_local_webserver=auth_local_webserver,
                auth_redirect_uri=auth_redirect_uri,
                client_id=client_id,
                client_secret=client_secret,
            )

        if self.project_id is None:
            self.project_id = default_project

        if self.project_id is None:
            raise ValueError("Could not determine project ID and one was not supplied.")

        # Cache the credentials if they haven't been set yet.
        if context.credentials is None:
            context.credentials = self.credentials
        if context.project is None:
            context.project = self.project_id

        self.client = _get_client(
            self.user_agent, self.rfc9110_delimiter, self.project_id, self.credentials
        )

    def _start_timer(self):
        self.start = time.time()

    def get_elapsed_seconds(self):
        return round(time.time() - self.start, 2)

    def log_elapsed_seconds(self, prefix="Elapsed", postfix="s.", overlong=6):
        sec = self.get_elapsed_seconds()
        if sec > overlong:
            logger.info("{} {} {}".format(prefix, sec, postfix))

    def get_client(self):
        import google.api_core.client_info

        bigquery = FEATURES.bigquery_try_import()

        user_agent = create_user_agent(
            user_agent=self.user_agent, rfc9110_delimiter=self.rfc9110_delimiter
        )

        client_info = google.api_core.client_info.ClientInfo(
            user_agent=user_agent,
        )
        return bigquery.Client(
            project=self.project_id,
            credentials=self.credentials,
            client_info=client_info,
        )

    @staticmethod
    def process_http_error(ex):
        # See `BigQuery Troubleshooting Errors
        # <https://cloud.google.com/bigquery/troubleshooting-errors>`__

        message = (
            ex.message.casefold()
            if hasattr(ex, "message") and ex.message is not None
            else ""
        )
        if "cancelled" in message:
            raise QueryTimeout("Reason: {0}".format(ex))
        elif "schema does not match" in message:
            error_message = ex.errors[0]["message"]
            raise InvalidSchema(f"Reason: {error_message}")
        elif "already exists: table" in message:
            error_message = ex.errors[0]["message"]
            raise TableCreationError(f"Reason: {error_message}")
        else:
            raise GenericGBQException("Reason: {0}".format(ex)) from ex

    def download_table(
        self,
        table_id: str,
        max_results: Optional[int] = None,
        progress_bar_type: Optional[str] = None,
        dtypes: Optional[Dict[str, Union[str, Any]]] = None,
    ) -> "pandas.DataFrame":
        from google.cloud import bigquery

        self._start_timer()

        try:
            table_ref = bigquery.TableReference.from_string(
                table_id, default_project=self.project_id
            )
            rows_iter = self.client.list_rows(table_ref, max_results=max_results)
        except self.http_error as ex:
            self.process_http_error(ex)

        return self._download_results(
            rows_iter,
            max_results=max_results,
            progress_bar_type=progress_bar_type,
            user_dtypes=dtypes,
        )

    def run_query(self, query, max_results=None, progress_bar_type=None, **kwargs):
        from google.cloud import bigquery

        job_config_dict = {
            "query": {
                "useLegacySql": self.dialect
                == "legacy"
                # 'allowLargeResults', 'createDisposition',
                # 'preserveNulls', destinationTable, useQueryCache
            }
        }
        config = kwargs.get("configuration")
        if config is not None:
            job_config_dict.update(config)

        timeout_ms = job_config_dict.get("jobTimeoutMs") or job_config_dict[
            "query"
        ].get("timeoutMs")

        if timeout_ms:
            timeout_ms = int(timeout_ms)
            # Having too small a timeout_ms results in individual
            # API calls timing out before they can finish.
            # ~300 milliseconds is rule of thumb for bare minimum
            # latency from the BigQuery API, however, 400 milliseconds
            # produced too many issues with flakybot failures.
            minimum_latency = 500
            if timeout_ms < minimum_latency:
                raise QueryTimeout(
                    f"Query timeout must be at least 500 milliseconds: timeout_ms equals {timeout_ms}."
                )
        else:
            timeout_ms = None

        self._start_timer()
        job_config = bigquery.QueryJobConfig.from_api_repr(job_config_dict)

        if FEATURES.bigquery_has_query_and_wait:
            rows_iter = pandas_gbq.query.query_and_wait_via_client_library(
                self,
                self.client,
                query,
                location=self.location,
                project_id=self.project_id,
                job_config=job_config,
                max_results=max_results,
                timeout_ms=timeout_ms,
            )
        else:
            rows_iter = pandas_gbq.query.query_and_wait(
                self,
                self.client,
                query,
                location=self.location,
                project_id=self.project_id,
                job_config=job_config,
                max_results=max_results,
                timeout_ms=timeout_ms,
            )

        dtypes = kwargs.get("dtypes")
        return self._download_results(
            rows_iter,
            max_results=max_results,
            progress_bar_type=progress_bar_type,
            user_dtypes=dtypes,
        )

    def _download_results(
        self,
        rows_iter,
        max_results=None,
        progress_bar_type=None,
        user_dtypes=None,
    ):
        # No results are desired, so don't bother downloading anything.
        if max_results == 0:
            return None

        if user_dtypes is None:
            user_dtypes = {}

        create_bqstorage_client = self.use_bqstorage_api
        if max_results is not None:
            create_bqstorage_client = False

        # If we're downloading a large table, BigQuery DataFrames might be a
        # better fit. Not all code paths will populate rows_iter._table, but
        # if it's not populated that means we are working with a small result
        # set.
        if (table_ref := getattr(rows_iter, "_table", None)) is not None:
            table = self.client.get_table(table_ref)
            if (
                isinstance((num_bytes := table.num_bytes), int)
                and num_bytes > pandas_gbq.constants.BYTES_TO_RECOMMEND_BIGFRAMES
            ):
                num_gib = num_bytes / pandas_gbq.constants.BYTES_IN_GIB
                warnings.warn(
                    f"Recommendation: Your results are {num_gib:.1f} GiB. "
                    "Consider using BigQuery DataFrames (https://bit.ly/bigframes-intro)"
                    "to process large results with pandas compatible APIs with transparent SQL "
                    "pushdown to BigQuery engine. This provides an opportunity to save on costs "
                    "and improve performance. "
                    "Please reach out to bigframes-feedback@google.com with any "
                    "questions or concerns. To disable this message, run "
                    "warnings.simplefilter('ignore', category=pandas_gbq.exceptions.LargeResultsWarning)",
                    category=pandas_gbq.exceptions.LargeResultsWarning,
                    # user's code
                    # -> read_gbq
                    # -> run_query
                    # -> download_results
                    stacklevel=4,
                )

        try:
            schema_fields = [field.to_api_repr() for field in rows_iter.schema]
            conversion_dtypes = _bqschema_to_nullsafe_dtypes(schema_fields)
            conversion_dtypes.update(user_dtypes)
            df = rows_iter.to_dataframe(
                dtypes=conversion_dtypes,
                progress_bar_type=progress_bar_type,
                create_bqstorage_client=create_bqstorage_client,
            )
        except self.http_error as ex:
            self.process_http_error(ex)

        df = _finalize_dtypes(df, schema_fields)

        logger.debug("Got {} rows.\n".format(rows_iter.total_rows))
        return df

    def load_data(
        self,
        dataframe,
        destination_table_ref,
        write_disposition,
        chunksize=None,
        schema=None,
        progress_bar=True,
        api_method: str = "load_parquet",
        billing_project: Optional[str] = None,
    ):
        from pandas_gbq import load

        total_rows = len(dataframe)

        try:
            chunks = load.load_chunks(
                self.client,
                dataframe,
                destination_table_ref,
                chunksize=chunksize,
                schema=schema,
                location=self.location,
                api_method=api_method,
                write_disposition=write_disposition,
                billing_project=billing_project,
            )
            if progress_bar and tqdm:
                chunks = tqdm.tqdm(chunks)
            for remaining_rows in chunks:
                logger.info(
                    "\r{} out of {} rows loaded.".format(
                        total_rows - remaining_rows, total_rows
                    )
                )
        except self.http_error as ex:
            self.process_http_error(ex)


def _bqschema_to_nullsafe_dtypes(schema_fields):
    """Specify explicit dtypes based on BigQuery schema.

    This function only specifies a dtype when the dtype allows nulls.
    Otherwise, use pandas's default dtype choice.

    See: http://pandas.pydata.org/pandas-docs/dev/missing_data.html
    #missing-data-casting-rules-and-indexing
    """
    import db_dtypes

    # If you update this mapping, also update the table at
    # `docs/reading.rst`.
    dtype_map = {
        "FLOAT": np.dtype(float),
        "INTEGER": "Int64",
        "TIME": db_dtypes.TimeDtype(),
        # Note: Other types such as 'datetime64[ns]' and db_types.DateDtype()
        # are not included because the pandas range does not align with the
        # BigQuery range. We need to attempt a conversion to those types and
        # fall back to 'object' when there are out-of-range values.
    }

    # Amend dtype_map with newer extension types if pandas version allows.
    if FEATURES.pandas_has_boolean_dtype:
        dtype_map["BOOLEAN"] = "boolean"

    dtypes = {}
    for field in schema_fields:
        name = str(field["name"])
        # Array BigQuery type is represented as an object column containing
        # list objects.
        if field["mode"].upper() == "REPEATED":
            dtypes[name] = "object"
            continue

        dtype = dtype_map.get(field["type"].upper())
        if dtype:
            dtypes[name] = dtype

    return dtypes


def _finalize_dtypes(
    df: "pandas.DataFrame", schema_fields: Sequence[Dict[str, Any]]
) -> "pandas.DataFrame":
    """
    Attempt to change the dtypes of those columns that don't map exactly.

    For example db_dtypes.DateDtype() and datetime64[ns] cannot represent
    0001-01-01, but they can represent dates within a couple hundred years of
    1970. See:
    https://github.com/googleapis/python-bigquery-pandas/issues/365
    """
    import db_dtypes
    import pandas.api.types

    # If you update this mapping, also update the table at
    # `docs/reading.rst`.
    dtype_map = {
        "DATE": db_dtypes.DateDtype(),
        "DATETIME": "datetime64[ns]",
        "TIMESTAMP": "datetime64[ns]",
    }

    for field in schema_fields:
        # This method doesn't modify ARRAY/REPEATED columns.
        if field["mode"].upper() == "REPEATED":
            continue

        name = str(field["name"])
        dtype = dtype_map.get(field["type"].upper())

        # Avoid deprecated conversion to timezone-naive dtype by only casting
        # object dtypes.
        if dtype and pandas.api.types.is_object_dtype(df[name]):
            df[name] = df[name].astype(dtype, errors="ignore")

    # Ensure any TIMESTAMP columns are tz-aware.
    df = pandas_gbq.timestamp.localize_df(df, schema_fields)

    return df


def _transform_read_gbq_configuration(configuration):
    """
    For backwards-compatibility, convert any previously client-side only
    parameters such as timeoutMs to the property name expected by the REST API.

    Makes a copy of configuration if changes are needed.
    """

    if configuration is None:
        return None

    timeout_ms = configuration.get("query", {}).get("timeoutMs")
    if timeout_ms is not None:
        # Transform timeoutMs to an actual server-side configuration.
        # https://github.com/googleapis/python-bigquery-pandas/issues/479
        configuration = copy.deepcopy(configuration)
        del configuration["query"]["timeoutMs"]
        configuration["jobTimeoutMs"] = timeout_ms

    return configuration


def read_gbq(
    query_or_table,
    project_id=None,
    index_col=None,
    columns=None,
    reauth=False,
    auth_local_webserver=True,
    dialect=None,
    location=None,
    configuration=None,
    credentials=None,
    use_bqstorage_api=False,
    max_results=None,
    verbose=None,
    private_key=None,
    progress_bar_type="tqdm",
    dtypes=None,
    auth_redirect_uri=None,
    client_id=None,
    client_secret=None,
    *,
    col_order=None,
    bigquery_client=None,
):
    r"""Read data from Google BigQuery to a pandas DataFrame.

    Run a SQL query in BigQuery or read directly from a table
    the `Python client library for BigQuery
    <https://cloud.google.com/python/docs/reference/bigquery/latest/index.html>`__
    and for `BigQuery Storage
    <https://cloud.google.com/python/docs/reference/bigquerystorage/latest>`__
    to make API requests.

    See the :ref:`How to authenticate with Google BigQuery <authentication>`
    guide for authentication instructions.

    .. note::
        Consider using `BigQuery DataFrames
        <https://cloud.google.com/bigquery/docs/dataframes-quickstart>`__ to
        process large results with pandas compatible APIs that run in the
        BigQuery SQL query engine. This provides an opportunity to save on
        costs and improve performance.

    Parameters
    ----------
    query_or_table : str
        SQL query to return data values. If the string is a table ID, fetch the
        rows directly from the table without running a query.
    project_id : str, optional
        Google Cloud Platform project ID. Optional when available from
        the environment.
    index_col : str, optional
        Name of result column to use for index in results DataFrame.
    columns : list(str), optional
        List of BigQuery column names in the desired order for results
        DataFrame.
    reauth : boolean, default False
        Force Google BigQuery to re-authenticate the user. This is useful
        if multiple accounts are used.
    auth_local_webserver : bool, default True
        Use the `local webserver flow
        <https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html#google_auth_oauthlib.flow.InstalledAppFlow.run_local_server>`_
        instead of the `console flow
        <https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html#google_auth_oauthlib.flow.InstalledAppFlow.run_console>`_
        when getting user credentials. Your code must run on the same machine
        as your web browser and your web browser can access your application
        via ``localhost:808X``.

        .. versionadded:: 0.2.0
    dialect : str, default 'standard'
        Note: The default value changed to 'standard' in version 0.10.0.

        SQL syntax dialect to use. Value can be one of:

        ``'legacy'``
            Use BigQuery's legacy SQL dialect. For more information see
            `BigQuery Legacy SQL Reference
            <https://cloud.google.com/bigquery/docs/reference/legacy-sql>`__.
        ``'standard'``
            Use BigQuery's standard SQL, which is
            compliant with the SQL 2011 standard. For more information
            see `BigQuery Standard SQL Reference
            <https://cloud.google.com/bigquery/docs/reference/standard-sql/>`__.
    location : str, optional
        Location where the query job should run. See the `BigQuery locations
        documentation
        <https://cloud.google.com/bigquery/docs/dataset-locations>`__ for a
        list of available locations. The location must match that of any
        datasets used in the query.

        .. versionadded:: 0.5.0
    configuration : dict, optional
        Query config parameters for job processing.
        For example:

            configuration = {'query': {'useQueryCache': False}}

        For more information see `BigQuery REST API Reference
        <https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs#configuration.query>`__.
    credentials : google.auth.credentials.Credentials, optional
        Credentials for accessing Google APIs. Use this parameter to override
        default credentials, such as to use Compute Engine
        :class:`google.auth.compute_engine.Credentials` or Service Account
        :class:`google.oauth2.service_account.Credentials` directly.

        .. versionadded:: 0.8.0
    use_bqstorage_api : bool, default False
        Use the `BigQuery Storage API
        <https://cloud.google.com/bigquery/docs/reference/storage/>`__ to
        download query results quickly, but at an increased cost. To use this
        API, first `enable it in the Cloud Console
        <https://console.cloud.google.com/apis/library/bigquerystorage.googleapis.com>`__.
        You must also have the `bigquery.readsessions.create
        <https://cloud.google.com/bigquery/docs/access-control#roles>`__
        permission on the project you are billing queries to.

        This feature requires the ``google-cloud-bigquery-storage`` and
        ``pyarrow`` packages.

        This value is ignored if ``max_results`` is set.

        .. versionadded:: 0.10.0
    max_results : int, optional
        If set, limit the maximum number of rows to fetch from the query
        results.

        .. versionadded:: 0.12.0
    progress_bar_type (Optional[str]):
        If set, use the `tqdm <https://tqdm.github.io/>`__ library to
        display a progress bar while the data downloads. Install the
        ``tqdm`` package to use this feature.
        Possible values of ``progress_bar_type`` include:

        ``None``
            No progress bar.
        ``'tqdm'``
            Use the :func:`tqdm.tqdm` function to print a progress bar
            to :data:`sys.stderr`.
        ``'tqdm_notebook'``
            Use the :func:`tqdm.tqdm_notebook` function to display a
            progress bar as a Jupyter notebook widget.
        ``'tqdm_gui'``
            Use the :func:`tqdm.tqdm_gui` function to display a
            progress bar as a graphical dialog box.
    dtypes : dict, optional
        A dictionary of column names to pandas ``dtype``. The provided
        ``dtype`` is used when constructing the series for the column
        specified. Otherwise, a default ``dtype`` is used.
    verbose : None, deprecated
        Deprecated in Pandas-GBQ 0.4.0. Use the `logging module
        to adjust verbosity instead
        <https://pandas-gbq.readthedocs.io/en/latest/intro.html#logging>`__.
    private_key : str, deprecated
        Deprecated in pandas-gbq version 0.8.0. Use the ``credentials``
        parameter and
        :func:`google.oauth2.service_account.Credentials.from_service_account_info`
        or
        :func:`google.oauth2.service_account.Credentials.from_service_account_file`
        instead.
    auth_redirect_uri : str
        Path to the authentication page for organization-specific authentication
        workflows. Used when ``auth_local_webserver=False``.
    client_id : str
        The Client ID for the Google Cloud Project the user is attempting to
        connect to.
    client_secret : str
        The Client Secret associated with the Client ID for the Google Cloud Project
        the user is attempting to connect to.
    col_order : list(str), optional
        Alias for columns, retained for backwards compatibility.
    bigquery_client : google.cloud.bigquery.Client, optional
        A Google Cloud BigQuery Python Client instance. If provided, it will be used for reading
        data, while the project and credentials parameters will be ignored.

    Returns
    -------
    df: DataFrame
        DataFrame representing results of query.
    """
    global context

    if dialect is None:
        dialect = context.dialect

    if dialect is None:
        dialect = "standard"

    _test_google_api_imports()

    if verbose is not None and FEATURES.pandas_has_deprecated_verbose:
        warnings.warn(
            "verbose is deprecated and will be removed in "
            "a future version. Set logging level in order to vary "
            "verbosity",
            FutureWarning,
            stacklevel=2,
        )

    if dialect not in ("legacy", "standard"):
        raise ValueError("'{0}' is not valid for dialect".format(dialect))

    configuration = _transform_read_gbq_configuration(configuration)

    if configuration and "query" in configuration and "query" in configuration["query"]:
        if query_or_table is not None:
            raise ValueError(
                "Query statement can't be specified "
                "inside config while it is specified "
                "as parameter"
            )
        query_or_table = configuration["query"].pop("query")

    connector = GbqConnector(
        project_id,
        reauth=reauth,
        dialect=dialect,
        auth_local_webserver=auth_local_webserver,
        location=location,
        credentials=credentials,
        private_key=private_key,
        use_bqstorage_api=use_bqstorage_api,
        auth_redirect_uri=auth_redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        bigquery_client=bigquery_client,
    )

    if _is_query(query_or_table):
        final_df = connector.run_query(
            query_or_table,
            configuration=configuration,
            max_results=max_results,
            progress_bar_type=progress_bar_type,
            dtypes=dtypes,
        )
    else:
        final_df = connector.download_table(
            query_or_table,
            max_results=max_results,
            progress_bar_type=progress_bar_type,
            dtypes=dtypes,
        )

    # Reindex the DataFrame on the provided column
    if index_col is not None:
        if index_col in final_df.columns:
            final_df.set_index(index_col, inplace=True)
        else:
            raise InvalidIndexColumn(
                'Index column "{0}" does not exist in DataFrame.'.format(index_col)
            )

    # Using columns as an alias for col_order, raising an error if both provided
    if col_order and not columns:
        columns = col_order
    elif col_order and columns:
        raise ValueError(
            "Must specify either columns (preferred) or col_order, not both"
        )

    # Change the order of columns in the DataFrame based on provided list
    # TODO(kiraksi): allow columns to be a subset of all columns in the table, with follow up PR
    if columns is not None:
        if sorted(columns) == sorted(final_df.columns):
            final_df = final_df[columns]
        else:
            raise InvalidColumnOrder("Column order does not match this DataFrame.")

    connector.log_elapsed_seconds(
        "Total time taken",
        datetime.now().strftime("s.\nFinished at %Y-%m-%d %H:%M:%S."),
    )

    return final_df


def to_gbq(
    dataframe,
    destination_table,
    project_id=None,
    chunksize=None,
    reauth=False,
    if_exists="fail",
    auth_local_webserver=True,
    table_schema=None,
    location=None,
    progress_bar=True,
    credentials=None,
    api_method: str = "default",
    verbose=None,
    private_key=None,
    auth_redirect_uri=None,
    client_id=None,
    client_secret=None,
    user_agent=None,
    rfc9110_delimiter=False,
    bigquery_client=None,
):
    """Write a DataFrame to a Google BigQuery table.

    The main method a user calls to export pandas DataFrame contents to Google BigQuery table.

    This method uses the Google Cloud client library to make requests to Google BigQuery, documented `here
    <https://googleapis.dev/python/bigquery/latest/index.html>`__.

    See the :ref:`How to authenticate with Google BigQuery <authentication>`
    guide for authentication instructions.

    Parameters
    ----------
    dataframe : pandas.DataFrame
        DataFrame to be written to a Google BigQuery table.
    destination_table : str
        Name of table to be written, in the form ``dataset.tablename`` or
        ``project.dataset.tablename``.
    project_id : str, optional
        Google Cloud Platform project ID. Optional when available from
        the environment.
    chunksize : int, optional
        Number of rows to be inserted in each chunk from the dataframe.
        Set to ``None`` to load the whole dataframe at once.
    reauth : bool, default False
        Force Google BigQuery to re-authenticate the user. This is useful
        if multiple accounts are used.
    if_exists : str, default 'fail'
        Behavior when the destination table exists. Value can be one of:

        ``'fail'``
            If table exists, do nothing.
        ``'replace'``
            If table exists, drop it, recreate it, and insert data.
        ``'append'``
            If table exists, insert data. Create if does not exist.
    auth_local_webserver : bool, default True
        Use the `local webserver flow
        <https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html#google_auth_oauthlib.flow.InstalledAppFlow.run_local_server>`_
        instead of the `console flow
        <https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html#google_auth_oauthlib.flow.InstalledAppFlow.run_console>`_
        when getting user credentials. Your code must run on the same machine
        as your web browser and your web browser can access your application
        via ``localhost:808X``.

        .. versionadded:: 0.2.0
    table_schema : list of dicts, optional
        List of BigQuery table fields to which according DataFrame
        columns conform to, e.g. ``[{'name': 'col1', 'type':
        'STRING'},...]``.  The ``type`` values must be BigQuery type names.

        - If ``table_schema`` is provided, it may contain all or a subset of
          DataFrame columns. If a subset is provided, the rest will be
          inferred from the DataFrame dtypes.  If ``table_schema`` contains
          columns not in the DataFrame, they'll be ignored.
        - If ``table_schema`` is **not** provided, it will be
          generated according to dtypes of DataFrame columns. See
          `Inferring the Table Schema
          <https://pandas-gbq.readthedocs.io/en/latest/writing.html#writing-schema>`__.
          for a description of the schema inference.

        See `BigQuery API documentation on valid column names
        <https://cloud.google.com/bigquery/docs/schemas#column_names`>__.

        .. versionadded:: 0.3.1
    location : str, optional
        Location where the load job should run. See the `BigQuery locations
        documentation
        <https://cloud.google.com/bigquery/docs/dataset-locations>`__ for a
        list of available locations. The location must match that of the
        target dataset.

        .. versionadded:: 0.5.0
    progress_bar : bool, default True
        Use the library `tqdm` to show the progress bar for the upload,
        chunk by chunk.

        .. versionadded:: 0.5.0
    credentials : google.auth.credentials.Credentials, optional
        Credentials for accessing Google APIs. Use this parameter to override
        default credentials, such as to use Compute Engine
        :class:`google.auth.compute_engine.Credentials` or Service Account
        :class:`google.oauth2.service_account.Credentials` directly.

        .. versionadded:: 0.8.0
    api_method : str, optional
        API method used to upload DataFrame to BigQuery. One of "load_parquet",
        "load_csv". Default "load_parquet" if pandas is version 1.1.0+,
        otherwise "load_csv".

        .. versionadded:: 0.16.0
    verbose : bool, deprecated
        Deprecated in Pandas-GBQ 0.4.0. Use the `logging module
        to adjust verbosity instead
        <https://pandas-gbq.readthedocs.io/en/latest/intro.html#logging>`__.
    private_key : str, deprecated
        Deprecated in pandas-gbq version 0.8.0. Use the ``credentials``
        parameter and
        :func:`google.oauth2.service_account.Credentials.from_service_account_info`
        or
        :func:`google.oauth2.service_account.Credentials.from_service_account_file`
        instead.
    auth_redirect_uri : str
        Path to the authentication page for organization-specific authentication
        workflows. Used when ``auth_local_webserver=False``.
    client_id : str
        The Client ID for the Google Cloud Project the user is attempting to
        connect to.
    client_secret : str
        The Client Secret associated with the Client ID for the Google Cloud Project
        the user is attempting to connect to.
    user_agent : str
        Custom user agent string used as a prefix to the pandas version.
    rfc9110_delimiter : bool
        Sets user agent delimiter to a hyphen or a slash.
        Default is False, meaning a hyphen will be used.
    bigquery_client : google.cloud.bigquery.Client, optional
        A Google Cloud BigQuery Python Client instance. If provided, it will be used for reading
        data, while the project, user_agent, and credentials parameters will be ignored.

        .. versionadded:: 0.23.3
    """

    # If we get a bigframes.pandas.DataFrame object, it may be possible to use
    # the code paths here, but it could potentially be quite expensive because
    # of the queries involved in type detection. It would be safer just to
    # fail early if there are bigframes-y methods available.
    # https://github.com/googleapis/python-bigquery-pandas/issues/824
    if hasattr(dataframe, "to_pandas") and hasattr(dataframe, "to_gbq"):
        raise TypeError(f"Expected a pandas.DataFrame, but got {repr(type(dataframe))}")

    _test_google_api_imports()

    from google.api_core import exceptions as google_exceptions
    from google.cloud import bigquery

    if verbose is not None and FEATURES.pandas_has_deprecated_verbose:
        warnings.warn(
            "verbose is deprecated and will be removed in "
            "a future version. Set logging level in order to vary "
            "verbosity",
            FutureWarning,
            stacklevel=1,
        )

    if api_method == "default":
        api_method = "load_parquet"

    if chunksize is not None:
        if api_method == "load_parquet":
            warnings.warn(
                "chunksize is ignored when using api_method='load_parquet'",
                DeprecationWarning,
                stacklevel=2,
            )
        else:
            warnings.warn(
                "chunksize will be ignored when using api_method='load_csv' in a future version of pandas-gbq",
                PendingDeprecationWarning,
                stacklevel=2,
            )

    if "." not in destination_table:
        raise NotFoundException(
            "Invalid Table Name. Should be of the form 'datasetId.tableId' or "
            "'projectId.datasetId.tableId'"
        )

    if if_exists not in ("fail", "replace", "append"):
        raise ValueError("'{0}' is not valid for if_exists".format(if_exists))

    if_exists_list = ["fail", "replace", "append"]
    dispositions = ["WRITE_EMPTY", "WRITE_TRUNCATE", "WRITE_APPEND"]
    dispositions_dict = dict(zip(if_exists_list, dispositions))

    write_disposition = dispositions_dict[if_exists]

    connector = GbqConnector(
        project_id,
        reauth=reauth,
        auth_local_webserver=auth_local_webserver,
        location=location,
        credentials=credentials,
        private_key=private_key,
        auth_redirect_uri=auth_redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        rfc9110_delimiter=rfc9110_delimiter,
        bigquery_client=bigquery_client,
    )
    bqclient = connector.client

    destination_table_ref = bigquery.table.TableReference.from_string(
        destination_table, default_project=connector.project_id
    )

    project_id_table = destination_table_ref.project
    dataset_id = destination_table_ref.dataset_id
    table_id = destination_table_ref.table_id

    default_schema = _generate_bq_schema(dataframe)
    # If table_schema isn't provided, we'll create one for you
    if not table_schema:
        table_schema = default_schema
    # It table_schema is provided, we'll update the default_schema to the provided table_schema
    else:
        table_schema = pandas_gbq.schema.update_schema(
            default_schema, dict(fields=table_schema)
        )

    try:
        # Try to get the table
        table = bqclient.get_table(destination_table_ref)
    except google_exceptions.NotFound:
        # If the table doesn't already exist, create it
        table_connector = _Table(
            project_id_table,
            dataset_id,
            location=location,
            credentials=connector.credentials,
        )
        table_connector.create(table_id, table_schema)
    else:
        if if_exists == "append":
            # Convert original schema (the schema that already exists) to pandas-gbq API format
            original_schema = pandas_gbq.schema.to_pandas_gbq(table.schema)

            # Update the local `table_schema` so mode (NULLABLE/REQUIRED)
            # matches. See: https://github.com/pydata/pandas-gbq/issues/315
            table_schema = pandas_gbq.schema.update_schema(
                table_schema, original_schema
            )

    if dataframe.empty:
        # Create the table (if needed), but don't try to run a load job with an
        # empty file. See: https://github.com/pydata/pandas-gbq/issues/237
        return

    connector.load_data(
        dataframe,
        destination_table_ref,
        write_disposition=write_disposition,
        chunksize=chunksize,
        schema=table_schema,
        progress_bar=progress_bar,
        api_method=api_method,
        billing_project=project_id,
    )


def generate_bq_schema(df, default_type="STRING"):
    """DEPRECATED: Given a passed df, generate the associated Google BigQuery
    schema.

    Parameters
    ----------
    df : DataFrame
    default_type : string
        The default big query type in case the type of the column
        does not exist in the schema.
    """
    # deprecation TimeSeries, #11121
    warnings.warn(
        "generate_bq_schema is deprecated and will be removed in " "a future version",
        FutureWarning,
        stacklevel=2,
    )

    return _generate_bq_schema(df, default_type=default_type)


def _generate_bq_schema(df, default_type="STRING"):
    """DEPRECATED: Given a dataframe, generate a Google BigQuery schema.

    This is a private method, but was used in external code to work around
    issues in the default schema generation. Now that individual columns can
    be overridden: https://github.com/pydata/pandas-gbq/issues/218, this
    method can be removed after there is time to migrate away from this
    method."""
    fields = pandas_gbq.schema.pandas_to_bigquery.dataframe_to_bigquery_fields(
        df,
        default_type=default_type,
    )
    fields_json = []

    for field in fields:
        fields_json.append(field.to_api_repr())

    return {"fields": fields_json}


class _Table(GbqConnector):
    def __init__(
        self,
        project_id,
        dataset_id,
        reauth=False,
        location=None,
        credentials=None,
        private_key=None,
    ):
        self.dataset_id = dataset_id
        super(_Table, self).__init__(
            project_id,
            reauth,
            location=location,
            credentials=credentials,
            private_key=private_key,
        )

    def _table_ref(self, table_id):
        """Return a BigQuery client library table reference"""
        from google.cloud.bigquery import DatasetReference, TableReference

        return TableReference(
            DatasetReference(self.project_id, self.dataset_id), table_id
        )

    def exists(self, table_id):
        """Check if a table exists in Google BigQuery

        Parameters
        ----------
        table : str
            Name of table to be verified

        Returns
        -------
        boolean
            true if table exists, otherwise false
        """
        from google.api_core.exceptions import NotFound

        table_ref = self._table_ref(table_id)
        try:
            self.client.get_table(table_ref)
            return True
        except NotFound:
            return False
        except self.http_error as ex:
            self.process_http_error(ex)

    def create(self, table_id, schema):
        """Create a table in Google BigQuery given a table and schema

        Parameters
        ----------
        table : str
            Name of table to be written
        schema : str
            Use the generate_bq_schema to generate your table schema from a
            dataframe.
        """
        from google.cloud.bigquery import DatasetReference, Table, TableReference

        if self.exists(table_id):
            raise TableCreationError("Table {0} already exists".format(table_id))

        if not _Dataset(self.project_id, credentials=self.credentials).exists(
            self.dataset_id
        ):
            _Dataset(
                self.project_id,
                credentials=self.credentials,
                location=self.location,
            ).create(self.dataset_id)

        table_ref = TableReference(
            DatasetReference(self.project_id, self.dataset_id), table_id
        )
        table = Table(table_ref)
        table.schema = pandas_gbq.schema.to_google_cloud_bigquery(schema)

        try:
            self.client.create_table(table)
        except self.http_error as ex:
            self.process_http_error(ex)

    def delete(self, table_id):
        """Delete a table in Google BigQuery

        Parameters
        ----------
        table : str
            Name of table to be deleted
        """
        from google.api_core.exceptions import NotFound

        table_ref = self._table_ref(table_id)
        try:
            self.client.delete_table(table_ref)
        except NotFound:
            # Ignore 404 error which may occur if table already deleted
            pass
        except self.http_error as ex:
            self.process_http_error(ex)


class _Dataset(GbqConnector):
    def __init__(
        self,
        project_id,
        reauth=False,
        location=None,
        credentials=None,
        private_key=None,
    ):
        super(_Dataset, self).__init__(
            project_id,
            reauth,
            credentials=credentials,
            location=location,
            private_key=private_key,
        )

    def _dataset_ref(self, dataset_id):
        """Return a BigQuery client library dataset reference"""
        from google.cloud.bigquery import DatasetReference

        return DatasetReference(self.project_id, dataset_id)

    def exists(self, dataset_id):
        """Check if a dataset exists in Google BigQuery

        Parameters
        ----------
        dataset_id : str
            Name of dataset to be verified

        Returns
        -------
        boolean
            true if dataset exists, otherwise false
        """
        from google.api_core.exceptions import NotFound

        try:
            self.client.get_dataset(self._dataset_ref(dataset_id))
            return True
        except NotFound:
            return False
        except self.http_error as ex:
            self.process_http_error(ex)

    def create(self, dataset_id):
        """Create a dataset in Google BigQuery

        Parameters
        ----------
        dataset : str
            Name of dataset to be written
        """
        from google.cloud.bigquery import Dataset

        if self.exists(dataset_id):
            raise DatasetCreationError(
                "Dataset {0} already " "exists".format(dataset_id)
            )

        dataset = Dataset(self._dataset_ref(dataset_id))

        if self.location is not None:
            dataset.location = self.location

        try:
            self.client.create_dataset(dataset)
        except self.http_error as ex:
            self.process_http_error(ex)


def create_user_agent(
    user_agent: Optional[str] = None, rfc9110_delimiter: bool = False
) -> str:
    """Creates a user agent string.

    The legacy format of our the user agent string was: `product-x.y.z` (where x,
    y, and z are the major, minor, and micro version numbers).

    Users are able to prepend this string with their own user agent identifier
    to render something similar to `<my_user_agent> pandas-x.y.z`.

    The legacy format used a hyphen to separate the product from the product
    version which differs slightly from the format recommended by RFC9110, which is:
    `product/x.y.z`. To produce a user agent more in line with the RFC, set
    rfc9110_delimiter to True. This setting does not depend on whether a
    user_agent is also supplied.

    Reference:
        https://www.rfc-editor.org/info/rfc9110

    Args:
        user_agent (Optional[str]): User agent string.

        rfc9110_delimiter (Optional[bool]): Sets delimiter to a hyphen or a slash.
        Default is False, meaning a hyphen will be used.

    Returns (str):
        Customized user agent string.

    Deprecation Warning:
        In a future major release, the default delimiter will be changed to
        a `/` in accordance with RFC9110.
    """
    import pandas as pd

    if rfc9110_delimiter:
        delimiter = "/"
    else:
        warnings.warn(
            "In a future major release, the default delimiter will be "
            "changed to a `/` in accordance with RFC9110.",
            PendingDeprecationWarning,
            stacklevel=2,
        )
        delimiter = "-"

    identity = f"pandas{delimiter}{pd.__version__}"

    if user_agent is None:
        user_agent = identity
    else:
        user_agent = f"{user_agent} {identity}"

    return user_agent


def _get_client(user_agent, rfc9110_delimiter, project_id, credentials):
    import google.api_core.client_info

    bigquery = FEATURES.bigquery_try_import()

    user_agent = create_user_agent(
        user_agent=user_agent, rfc9110_delimiter=rfc9110_delimiter
    )

    client_info = google.api_core.client_info.ClientInfo(
        user_agent=user_agent,
    )
    return bigquery.Client(
        project=project_id,
        credentials=credentials,
        client_info=client_info,
    )
