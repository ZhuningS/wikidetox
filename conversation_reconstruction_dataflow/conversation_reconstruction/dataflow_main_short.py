# -*- coding: utf-8 -*-
"""
Copyright 2017 Google Inc.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

-------------------------------------------------------------------------------

A dataflow pipeline to reconstruct conversations on Wikipedia talk pages from ingested json files.

Run with:

python dataflow_main.py --setup_file ./setup.py
"""
from __future__ import absolute_import
import argparse
import logging
import subprocess
import json
from os import path
import urllib2
import traceback
from google.cloud import bigquery as bigquery_op 

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.io.gcp import bigquery as bigquery_io 
from construct_utils.conversation_constructor import Conversation_Constructor


LOG_INTERVAL = 1000

def run(known_args, pipeline_args):
  """Main entry point; defines and runs the reconstruction pipeline."""

  pipeline_args.extend([
    '--runner=DataflowRunner',
    '--project=wikidetox-viz',
    '--staging_location=gs://wikidetox-viz-dataflow/staging',
    '--temp_location=gs://wikidetox-viz-dataflow/tmp',
    '--job_name=reconstruction-short-pages-week{lw}year{ly}-week{uw}year{uy}'.format(lw=known_args.lower_week, ly=known_args.lower_year, uw=known_args.upper_week, uy=known_args.upper_year),
    '--num_workers=30',
    '--extra_package=third_party/mwparserfromhell.tar.gz'
  ])
  pipeline_options = PipelineOptions(pipeline_args)
  pipeline_options.view_as(SetupOptions).save_main_session = True
  jobname = 'reconstruction-short-pages-week{lw}year{ly}-week{uw}year{uy}'.format(lw=known_args.lower_week, ly=known_args.lower_year, uw=known_args.upper_week, uy=known_args.upper_year)

  debug_page =''#'and page_id = \'37130483\''
  debug1 =''#'where page_id = \'37130483\''

  within_time_range = '((week >= {lw} and year = {ly}) or year > {ly}) and ((week <= {uw} and year = {uy}) or year < {uy})'.format(lw = known_args.lower_week, ly = known_args.lower_year, uw = known_args.upper_week, uy = known_args.upper_year)
  before_time_range = '(week < {lw} and year = {ly}) or year < {ly}'.format(lw=known_args.lower_week, ly=known_args.lower_year) 
  ingested_revs_for_processing = "WITH revs AS (SELECT * FROM {input_table} WHERE {time_range} {debug}) SELECT page_id, ARRAY_AGG(revs ORDER BY timestamp, rev_id_in_int) AS cur_rev FROM revs GROUP BY page_id LIMIT 10".format(input_table=known_args.input_table, time_range=within_time_range, debug=debug_page)
  last_revision_processed = "WITH revs AS (SELECT * FROM {input_table} WHERE {before_time_range} {debug}) SELECT page_id, ARRAY_AGG(revs ORDER BY timestamp DESC, rev_id_in_int DESC LIMIT 1)[OFFSET(0)] AS last_rev FROM revs GROUP BY page_id LIMIT 1".format(input_table=known_args.input_table, before_time_range=before_time_range, debug=debug_page)
  last_page_state = "WITH page_states AS (SELECT * FROM {page_state_table} {debug}) SELECT page_id, ARRAY_AGG(page_states ORDER BY timestamp DESC, rev_id DESC LIMIT 1)[OFFSET(0)] AS last_page_state FROM page_states GROUP BY page_id LIMIT 1".format(page_state_table=known_args.input_page_state_table, debug=debug1)
  groupby_mapping = lambda x: (x['page_id'], x)
  with beam.Pipeline(options=pipeline_options) as p:
    if known_args.read_input_from_cloud:
       groupby_mapping = lambda x: (json.loads(x)['page_id'], json.loads(x))
       to_be_processed = (p | 'Read_to_be_processed' >> beam.io.ReadFromText("gs://wikidetox-viz-dataflow/testing/%s.input_revs*"%jobname)| 'INGESTED_assign_page_id_as_key' >> beam.Map(groupby_mapping))
       # Read from ingested table to get revisions to process
       last_revision = (p | 'Retrieve_last_revision' >> beam.io.ReadFromText("gs://wikidetox-viz-dataflow/testing/%s.last_rev*"%jobname)| 'LASTREV_assign_page_id_as_key' >> beam.Map(groupby_mapping))
       # Read from ingested table to get last processed revision 
       page_state = (p | 'Retrieve_page_state' >> beam.io.ReadFromText("gs://wikidetox-viz-dataflow/testing/%s.page_states*"%jobname)| 'PAGESTATE_assign_page_id_as_key' >> beam.Map(groupby_mapping))
       # Read from page state table to get the page states recorded from previous processing steps
       mapping = lambda x: (json.loads(x)['page_id'], json.loads(x))
    else:
       to_be_processed = (p | 'Read_to_be_processed' >> beam.io.Read(beam.io.BigQuerySource(query=ingested_revs_for_processing, validate=True, use_standard_sql=True))
                            | 'INGESTED_assign_page_id_as_key' >> beam.Map(groupby_mapping))
       # Read from ingested table to get revisions to process
       last_revision = (p 
            | 'Retrieve_last_revision' >> beam.io.Read(beam.io.BigQuerySource(query=last_revision_processed, validate=True, use_standard_sql=True))
            | 'LASTREV_assign_page_id_as_key' >> beam.Map(groupby_mapping))
       # Read from ingested table to get last processed revision 
       page_state = (p | 'Retrieve_page_state' >> beam.io.Read(beam.io.BigQuerySource(query=last_page_state, validate=True, use_standard_sql=True))
                       | 'PAGESTATE_assign_page_id_as_key' >> beam.Map(groupby_mapping))
    # Read from page state table to get the page states recorded from previous processing steps

    reconstruction_results, page_states, last_rev, input_ps, input_rev\
                   = ({'to_be_processed': to_be_processed, 'last_revision': last_revision, 'page_state': page_state}
                   | beam.CoGroupByKey()
                   # Join information based on page_id
                   | beam.ParDo(ReconstructConversation()).with_outputs('page_states', 'last_revision', 'input_page_state', 'to_be_processed_revision', main = 'reconstruction_results'))
                   # Reconstruct the conversations
    if known_args.save_res_to_cloud:
       page_states | "WritePageStates" >> beam.io.WriteToText("gs://wikidetox-viz-dataflow/%s/page_states"%jobname) 
       reconstruction_results | "WriteReconstructedResults" >> beam.io.WriteToText("gs://wikidetox-viz-dataflow/%s/reconstructed"%jobname) 
    else:
       page_states | "WritePageStates" >> beam.io.Write(bigquery_io.BigQuerySink(known_args.page_states_output_table, schema=known_args.page_states_output_schema, write_disposition='WRITE_APPEND', validate=True))
       # Write the page states to BigQuery
       reconstruction_results | "WriteReconstructedResults" >> beam.io.Write(bigquery_io.BigQuerySink(known_args.output_table, schema=known_args.output_schema, write_disposition='WRITE_APPEND', validate=True))
       # Write the reconstructed results to BigQuery
    if known_args.save_input_to_cloud_storage:
       last_rev | "WriteBackInput_last_rev" >> beam.io.WriteToText("gs://wikidetox-viz-dataflow/testing/%s.last_rev"%jobname)
       input_ps | "WriteBackInput_page_states" >> beam.io.WriteToText("gs://wikidetox-viz-dataflow/testing/%s.page_states"%jobname)
       input_rev | "WriteBackInput_input_revisions" >> beam.io.WriteToText("gs://wikidetox-viz-dataflow/testing/%s.input_revs"%jobname)


class ReconstructConversation(beam.DoFn):
  def process(self, info):
    (page_id, data) = info
    if not(page_id == '31149745'):
       return
    if known_args.read_input_from_cloud:
       rows = data['to_be_processed']
       last_revision = data['last_revision']
       page_state = data['page_state']
       if not(page_state == []):
          last_revision = last_revision[0]['last_rev']
          page_state = page_state[0]['last_page_state']
    else:
       if data['to_be_processed'] == []: return
       rows = data['to_be_processed'][0]['cur_rev']
       if not(data['page_state'] == []):
          last_revision = data['last_revision'][0]['last_rev']
          page_state = data['page_state'][0]['last_page_state']
       else:
          last_revision = []
          page_state = []
       if 'Archive' in rows[0]['page_title']: return 

    if rows == []: return 
    # Return when no revisions need to be processed for this page
    if page_id == None: return
    logging.info('USERLOG: Reconstruction work start on page: %s'%page_id)
    processor = Conversation_Constructor()
    if not(page_state) == []:
       if known_args.save_input_to_cloud_storage: 
          yield beam.pvalue.TaggedOutput('last_revision', json.dumps(last_revision))
          yield beam.pvalue.TaggedOutput('input_page_state', json.dumps(page_state))
       logging.info('Page %s existed: loading page state, last revision: %s'%(page_id, last_revision['rev_id'])) 
       processor.load(page_state['page_state'], page_state['deleted_comments'], page_state['conversation_id'], page_state['authors'], last_revision['text'])
    revision = {}
    last_revision = 'None'
    error_encountered = False
    cnt = 0
    for cur_revision in rows:
        if not('rev_id' in cur_revision): continue
        if cur_revision['record_index'] == 0: 
           revision = cur_revision
        else:
           revision['text'] += cur_revision['text']
        if cur_revision['record_index'] == cur_revision['records_count'] - 1:
           cnt += 1
           last_revision = revision['rev_id']
           if known_args.save_input_to_cloud_storage:
              actions = []
           else:
              try:
                 page_state, actions = processor.process(revision, DEBUGGING_MODE = False)
              except: 
                 logging.error('ERRORLOG: Reconstruction on page %s failed! last revision: %s' %(page_id, last_revision))
                 raise ValueError
           last_page_state = page_state 
           for action in actions:
               if not(known_args.save_res_to_cloud):
                  yield action
               else:
                  yield json.dumps(action)
           if cnt % LOG_INTERVAL == 0 and not(known_args.save_res_to_cloud):
              yield beam.pvalue.TaggedOutput('page_states', page_state)
        if known_args.save_input_to_cloud_storage: 
           yield beam.pvalue.TaggedOutput('to_be_processed_revision', json.dumps(cur_revision))
    if last_page_state and not(known_args.save_res_to_cloud):
       yield beam.pvalue.TaggedOutput('page_states', last_page_state)
    if (known_args.save_res_to_cloud):
       yield beam.pvalue.TaggedOutput('page_states', json.dumps(last_page_state))
    if not(error_encountered):
       logging.info('USERLOG: Reconstruction on page %s complete! last revision: %s' %(page_id, last_revision))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  parser = argparse.ArgumentParser()
  parser.add_argument('--save_res_to_bigQuery',
                      dest='save_res_to_cloud',
                      action='store_false',
                      help='Save the result to BigQuery.')
  parser.add_argument('--not_save_input',
                      dest='save_input_to_cloud_storage',
                      action='store_false',
                      help='Save the inputs to Cloud.')
  parser.add_argument('--load_input_from_cloud',
                      dest='read_input_from_cloud',
                      action='store_true',
                      help='Read input from Cloud.')


  # Input BigQuery Table
  parser.add_argument('--input_table',
                      dest='input_table',
                      default='wikidetox_conversations.page_31149745',
                      help='Input table with ingested revisions.')

  parser.add_argument('--input_page_state_table',
                      dest='input_page_state_table',
                      default='wikidetox_conversations.page_states_short',
                      help='Input page states table from previous reconstruction process.')

  parser.add_argument('--week',
                      dest='week',
                      default=None,
                      help='The week of data you want to process')
  parser.add_argument('--year',
                      dest='year',
                      default=None,
                      help='The year that the week is in')
  parser.add_argument('--week_lowerbound',
                      dest='lower_week',
                      default=None,
                      help='The start week of data you want to process')
  parser.add_argument('--year_lowerbound',
                      dest='lower_year',
                      default=None,
                      help='The year of the start week.')
  parser.add_argument('--week_upperbound',
                      dest='upper_week',
                      default=None,
                      help='The end week of data you want to process')
  parser.add_argument('--year_upperbound',
                      dest='upper_year',
                      default=None,
                      help='The year of the end week.')
 
  # Ouput BigQuery Table

  page_states_output_schema = 'rev_id:INTEGER, page_id:STRING, page_state:STRING, deleted_comments:STRING, conversation_id:STRING, authors:STRING, timestamp:STRING'  
  parser.add_argument('--page_states_output_table',
                      dest='page_states_output_table',
                      default='wikidetox-viz:wikidetox_conversations.page_states_short',
                      help='Output page state table for reconstruction.')
  parser.add_argument('--page_states_output_schema',
                      dest='page_states_output_schema',
                      default=page_states_output_schema,
                      help='Page states output table schema.')

  output_schema = 'user_id:STRING, user_text:STRING, timestamp:STRING, content:STRING, parent_id:STRING, replyTo_id:STRING, indentation:INTEGER, page_id:STRING, page_title:STRING, type:STRING, id:STRING, rev_id:STRING, conversation_id:STRING, authors:STRING'  
  parser.add_argument('--output_schema',
                      dest='output_schema',
                      default=output_schema,
                      help='Output table schema.')
  parser.add_argument('--initial_reconstruction',
                      dest='initial_reconstruction',
                      default=False,
                      help='Is this the first time reconstruction, meaning no existing page states processed.')

  known_args, pipeline_args = parser.parse_known_args()
  if known_args.week:
     known_args.lower_week, known_args.upper_week = int(known_args.week), int(known_args.week)
     known_args.lower_year, known_args.upper_year = int(known_args.year), int(known_args.year)
  known_args.lower_week = int(known_args.lower_week)
  known_args.lower_year = int(known_args.lower_year) 
  known_args.upper_week = int(known_args.upper_week)
  known_args.upper_year = int(known_args.upper_year)
  known_args.output_table = 'wikidetox-viz:wikidetox_conversations.reconstructed_short_pages'
  run(known_args, pipeline_args)
