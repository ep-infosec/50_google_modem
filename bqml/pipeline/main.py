# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Main pipeline code.
"""

import datetime
import urllib

from googleapiclient import discovery
from googleapiclient.http import MediaFileUpload
import httplib2
from oauth2client.service_account import ServiceAccountCredentials
import params
import requests
from retrying import retry
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from google.cloud import bigquery

GA_ACCOUNT_ID = params.GA_ACCOUNT_ID
GA_PROPERTY_ID = params.GA_PROPERTY_ID
GA_DATASET_ID = params.GA_DATASET_ID
GA_IMPORT_METHOD = params.GA_IMPORT_METHOD
BQML_PREDICT_QUERY = params.BQML_PREDICT_QUERY
GA_MP_STANDARD_HIT_DETAILS = params.GA_MP_STANDARD_HIT_DETAILS

ENABLED_LOGGING = params.ENABLE_BQ_LOGGING
ENABLED_EMAIL = params.ENABLE_SENDGRID_EMAIL_REPORTING

LOGS_BQ_TABLE = "{0}.{1}.{2}".format(params.GCP_PROJECT_ID,
                                     params.BQ_DATASET_NAME,
                                     params.BQ_TABLE_NAME)

SENDGRID_API_KEY = params.SENDGRID_API_KEY
TO_EMAIL = params.TO_EMAIL
FROM_EMAIL = params.FROM_EMAIL
SUBJECT = params.SUBJECT
HTML_CONTENT = params.HTML_CONTENT

SERVICE_ACCOUNT_FILE = "svc_key.json"
CSV_LOCATION = "/tmp/data.csv"

GA_MP_ENDPOINT = "https://www.google-analytics.com/batch"

GA_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly",
             "https://www.googleapis.com/auth/analytics.edit",
             "https://www.googleapis.com/auth/analytics"]
GA_API_NAME = "analytics"
GA_API_VERSION = "v3"

CLOUD_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def authorize_ga_api():
  """Fetches the GA API obj.

  Returns:
    ga_api: GA API obj.
  """
  ga_credentials = ServiceAccountCredentials.from_json_keyfile_name(
      SERVICE_ACCOUNT_FILE, GA_SCOPES)
  http = ga_credentials.authorize(http=httplib2.Http())
  ga_api = discovery.build(GA_API_NAME, GA_API_VERSION, http=http)
  return ga_api


def read_from_bq():
  """Reads the prediction query from Bigquery using BQML.

  Returns:
    dataframe: BQML model results dataframe.
  """

  bq_client = bigquery.Client()
  query_job = bq_client.query(BQML_PREDICT_QUERY)
  results = query_job.result()
  dataframe = results.to_dataframe()
  if GA_IMPORT_METHOD == "di":
    # assumes columns in BQ are named as ga_<name> e.g. ga_dimension1
    # converts them to ga:clientId, ga:dimension1
    dataframe.columns = [col_name.replace("_", ":")
                         for col_name in dataframe.columns.values]
  return dataframe


def write_df_to_csv(df):
  """Converts BQML model results to CSV.

  Args:
    df: final results dataframe for GA export.
  """
  csv_string = df.to_csv(index=False)
  with open(CSV_LOCATION, "w+") as f:
    f.write(csv_string)


def write_to_ga_via_di(ga_api):
  """Write the prediction results into GA via data import.

  Args:
    ga_api: Google Analytics Management API object.
  """
  media = MediaFileUpload(CSV_LOCATION,
                          mimetype="application/octet-stream",
                          resumable=False)
  ga_api.management().uploads().uploadData(
      accountId=GA_ACCOUNT_ID,
      webPropertyId=GA_PROPERTY_ID,
      customDataSourceId=GA_DATASET_ID,
      media_body=media).execute()


def delete_ga_prev_uploads(ga_api):
  """Delete previous GA data import files.

  Args:
    ga_api: Google Analytics Management API object.
  """
  response = ga_api.management().uploads().list(
      accountId=GA_ACCOUNT_ID,
      webPropertyId=GA_PROPERTY_ID,
      customDataSourceId=GA_DATASET_ID).execute()
  uploads = response["items"]
  cids = [upload["id"] for upload in uploads[1:]]
  delete_request_body = {"customDataImportUids": cids}
  ga_api.management().uploads().deleteUploadData(
      accountId=GA_ACCOUNT_ID,
      webPropertyId=GA_PROPERTY_ID,
      customDataSourceId=GA_DATASET_ID,
      body=delete_request_body).execute()


def send_mp_hit(payload_send, success_requests, failed_requests):
  """Send hit to Measurement Protocol endpoint.

  Args:
      payload_send: Measurement Protocol hit package to send
      success_requests: list of successful batch requests to GA
      failed_requests:  list of failed batch requests to GA
  Returns:
      boolean
  """

  # Submit a POST request to Measurement Protocol endpoint
  prepared = requests.Request("POST",
                              GA_MP_ENDPOINT,
                              data=payload_send).prepare()
  print("Sending measurement protcol request to url " + prepared.url)

  response = requests.Session().send(prepared)

  if response.status_code not in range(200, 299):
    print("Measurement Protocol submission unsuccessful status code: " +
          str(response.status_code))
    failed_requests.append(payload_send)
    return success_requests, failed_requests

  print("Measurement Protocol submission status code: " +
        str(response.status_code))
  success_requests.append(payload_send)
  return success_requests, failed_requests


def prepare_payloads_for_batch_request(payloads):
  """Merges payloads to send them in a batch request.

  Args:
    payloads: list of payload, each payload being a dictionary.

  Returns:
    concatenated url-encoded payloads. For example:
        param1=value10&param2=value20
        param1=value11&param2=value21
  """
  assert isinstance(payloads, list) or isinstance(payloads, tuple)
  payloads_utf8 = [sorted([(k, str(p[k]).encode("utf-8")) for k in p],
                          key=lambda t: t[0]) for p in payloads]
  return "\n".join(map(lambda p: urllib.parse.urlencode(p), payloads_utf8))


def write_to_ga_via_mp(df):
  """Write the prediction results into GA via Measurement Protocol.

  Args:
    df: BQML model results dataframe
  """
  i = 0
  success_requests, failed_requests, payload_list = list(), list(), list()

  for row_index, bq_results_row in df.iterrows():
    i += 1
    hit_data = {}
    for (ga_key, value) in GA_MP_STANDARD_HIT_DETAILS.items():
      if value:
        hit_data[ga_key] = value

    # add additional information from BQ
    for (column_name, column_value) in bq_results_row.iteritems():
      hit_data[column_name] = column_value

    payload_list.append(hit_data)

    if i%20 == 0:
      # batch the hits up
      payload_send = prepare_payloads_for_batch_request(payload_list)
      print("Payload to send: " + payload_send)
      success_requests, failed_requests = send_mp_hit(payload_send,
                                                      success_requests,
                                                      failed_requests)
      payload_list = list()
      i = 0

  # Issue last batch call
  if i > 0:
    print("Sending remaining items to GA")
    payload_send = prepare_payloads_for_batch_request(payload_list)
    print("Payload to send: " + payload_send)
    success_requests, failed_requests = send_mp_hit(payload_send,
                                                    success_requests,
                                                    failed_requests)

  print("Completed all GA calls. Total successful batches:  " +
        str(len(success_requests)) + " and failed batches: " +
        str(len(failed_requests)))
  if failed_requests:
    print("Failed request details: " + failed_requests)


# Retry 2^x * 1000 milliseconds between each retry, up to 10 seconds
# ,then 10 seconds afterwards - for 5 attempts
@retry(stop_max_attempt_number=5,
       wait_exponential_multiplier=1000, wait_exponential_max=10000)
def write_to_bq_logs(status, message):
  """Write to BQ Logs.

  Args:
    status: status of the workflow run - SUCCESS or ERROR
    message: Error message, if there's an error
  """
  bq_client = bigquery.Client()
  timestamp_utc = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  bq_insert_template_query = """INSERT INTO `{0}` VALUES ("{1}","{2}","{3}")"""
  write_logs_query = bq_insert_template_query.format(LOGS_BQ_TABLE,
                                                     timestamp_utc,
                                                     status, message)
  bq_client.query(write_logs_query)


# Retry 2^x * 1000 milliseconds between each retry, up to 10 seconds
# ,then 10 seconds afterwards - for 5 attempts
@retry(stop_max_attempt_number=5,
       wait_exponential_multiplier=1000, wait_exponential_max=10000)
def send_email(error_message):
  """Delete previous GA data import files.

  Args:
    error_message: Error message.

  Raises:
    Exception: An exception for failed emails.
  """
  timestamp_utc = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  email_content = HTML_CONTENT.format(timestamp_utc, error_message)
  message = Mail(from_email=FROM_EMAIL,
                 to_emails=TO_EMAIL,
                 subject=SUBJECT,
                 html_content=email_content)
  sg = SendGridAPIClient(SENDGRID_API_KEY)
  response = sg.send(message)
  if str(response.status_code)[0] != "2":
    raise Exception("Email not sent.")


def trigger_workflow(request):
  """Code to trigger workflow.

  Args:
    request: HTTP request object.
  Returns:
    workflow_status: Success or Error.
  """
  timestamp_utc = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  try:
    df = read_from_bq()
    if GA_IMPORT_METHOD == "di":
      write_df_to_csv(df)
      ga_api = authorize_ga_api()
      write_to_ga_via_di(ga_api)
      delete_ga_prev_uploads(ga_api)
    elif GA_IMPORT_METHOD == "mp":
      write_to_ga_via_mp(df)
    else:
      raise Exception("GA Export Method not found.")
    if ENABLED_LOGGING:
      write_to_bq_logs(status="SUCCESS", message="")
    message = "{0},SUCCESS".format(timestamp_utc)
    return message
  except Exception as e:
    if ENABLED_LOGGING:
      write_to_bq_logs(status="ERROR", message=str(e))
    if ENABLED_EMAIL:
      send_email(error_message=str(e))
    message = "{0},ERROR,{1}".format(timestamp_utc, str(e))
    return message


print(trigger_workflow(request=None))
