/*
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
*/
export interface Config {
  port: string;
  staticPath: string;
  cloudProjectId: string;
  crowd9ApiUrl : string;
  clientJobKey : string;
  // Note: this is default value, but is overwritten
  // by the env variables when NODE_ENV=production in run_server.ts
  isProduction : boolean;
}