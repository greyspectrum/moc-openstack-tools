#   Copyright 2016 Massachusetts Open Cloud
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
"""Update a project's OpenStack quotas using Google Sheets data

For each entry in the Google Sheet, the script will:
    - update the specified quotas
    - generate an email describing the update
    - send the email to the requestor, cc'ing all project users and anyone
      else specified in the config file cc_list parameter

Usage:
    python set-quotas.py
"""
import ConfigParser
from keystoneclient.v3 import client
from keystoneauth1.identity import v3
from keystoneauth1 import session

from quotas import QuotaManager
from message import TemplateMessage
import spreadsheet


class ProjectNotFoundError(Exception):
    """The specified project does not exist"""
    def __init__(self, project_name):
        message = 'Cannot find project: {}'.format(project_name)
        super(ProjectNotFoundError, self).__init__(message)


def parse_rows(rows):
    """Parse quota update data from the spreadsheet into a dictionary
    
    Expects 'rows' to include all rows, with row 0 being the header row
    Returns a dictionary of projects/quotas.
    """
    # NOTE: entry[16] is a required field in the Google Form, so it is safe
    # to assume entry[0:15] exists.

    project_list = []
    for idx, entry in enumerate(rows):
        # ignore row 0 (the header row) and blank rows
        if (idx == 0) or (entry == []):
            continue
        else:
            project = dict()
            project['user_fullname'] = entry[1]
            # entry[2] is organization
            # entry[3] is Openstack username (deprecated)
            project['name'] = entry[4]
            # entry[5] is Type of Increase
            # entry[6] is End Date
            # entry[7] is the Comments from the Temporary page (deprecated)
            quotas = {'instances': entry[8],
                      'cores': entry[9],
                      # form requests RAM amount in GB so the users don't
                      # get confused over multiplying by 1000 vs 1024
                      'ram': entry[10] * 1024,
                      'floatingip': entry[11],
                      'volumes': entry[12],
                      'snapshots': entry[13],
                      'gigabytes': entry[14]}
            
            unchanged_quotas = [q for q in quotas if quotas[q] == '']
            for quota_name in unchanged_quotas:
                    del quotas[quota_name]
            project['quotas'] = quotas

            # entry[15] is Comments - required field
            project['email'] = entry[16].replace(u'\xa0', ' ').strip()
            project['row'] = idx

            project_list.append(project)

    return project_list


def build_quota_table(old_quotas, updated_quotas):
    """Construct the table of updated quotas for insertion into the email"""
      
    QUOTA_FORMAT = "{quota:>25} |{old_value:>10} |{new_value:>10}"
    
    headers = QUOTA_FORMAT.format(quota='QUOTA  ', old_value='OLD VALUE',
                                  new_value='NEW VALUE')
    dividers = QUOTA_FORMAT.format(quota='-' * 20, old_value='-' * 10,
                                   new_value='-' * 10)
    
    quota_rows = [headers, dividers]
    for q in updated_quotas:
        quota_rows.append(QUOTA_FORMAT.format(quota=q,
                                              old_value=old_quotas[q],
                                              new_value=updated_quotas[q]))
    
    quota_table = "\n".join(quota_rows)
    return quota_table


def match_keystone_project(all_ks_projects, form_project):
    """Match the project specified by the user to a keystone project"""
    ks_project = [project for project in all_ks_projects
                  if form_project.lower() == project.name.lower()]
    
    if not ks_project:
        raise ProjectNotFoundError(form_project)
    else:
        return ks_project[0]


if __name__ == "__main__":

    # configuration
    config = ConfigParser.ConfigParser()
    config.read('settings.ini')
    admin_user = config.get('auth', 'admin_user')
    admin_pwd = config.get('auth', 'admin_pwd')
    admin_project = config.get('auth', 'admin_project')
    auth_url = config.get('auth', 'auth_url')
    nova_version = config.get('nova', 'version')
    quota_auth_file = config.get('quota_sheet', 'auth_file')
    quota_worksheet_key = config.get('quota_sheet', 'worksheet_key')
    quota_template = config.get('quota_email', 'template')

    # openstack auth
    auth = v3.Password(auth_url=auth_url,
                       username=admin_user,
                       user_domain_id='default',
                       project_name=admin_project,
                       project_domain_id='default',
                       password=admin_pwd)
    session = session.Session(auth=auth)
    keystone = client.Client(session=session)
    all_ks_projects = keystone.projects.list()
    quota_manager = QuotaManager(session=session, nova_version=nova_version)
    
    # get data from Google Sheet
    sheet = spreadsheet.Spreadsheet(quota_auth_file, quota_worksheet_key)
    rows = sheet.get_all_rows("Form Responses 1")
    project_list = parse_rows(rows)
    bad_rows = []
    
    # NOTE: 'project' is the project data from Google Sheets
    # and 'ks_project' is the matching project resource from Keystone
    for project in project_list:
        try:
            ks_project = match_keystone_project(all_ks_projects,
                                                project['name'])
        except ProjectNotFoundError as err:
            print err.message
            bad_rows.append(project['row'])
            continue
        
        old_quotas = quota_manager.get_current(ks_project.id)

        print "updating the following quotas for project {}:\n\t{}".format(
              ks_project.name, project['quotas'].keys())
        
        new_quotas = quota_manager.modify_quotas(ks_project.id,
                                                 **project['quotas'])
    
        quota_cfg = dict(config.items('quota_email'))
        quota_cfg['subject'] = quota_cfg['subject'].format(
            project=ks_project.name)
        
        # add the emails of the project's other users to the CC list
        roles = keystone.role_assignments.list(project=ks_project.id)
        for role in roles:
            user = keystone.users.get(role.user['id'])
            if user.email != project['email']:
                quota_cfg['cc_list'] = ", ".join([quota_cfg['cc_list'],
                                                  user.email])

        quota_list = build_quota_table(old_quotas, project['quotas'])
        msg = TemplateMessage(email=project['email'],
                              fullname=project['user_fullname'],
                              project=ks_project.name,
                              quota_list=quota_list,
                              **quota_cfg)
        msg.send()
        print "Successfully updated quotas for project {}".format(
              ks_project.name)

    if bad_rows:
        print "WARNING: The following rows were not processed: {}".format(
              bad_rows)