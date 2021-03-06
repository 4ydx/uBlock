#!/usr/bin/env python3

import datetime
import json
import jwt
import os
import re
import requests
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from distutils.version import StrictVersion
from string import Template

# - Download target (raw) uBlock0.webext.xpi from GitHub
#   - This is referred to as "raw" package
#   - This will fail if not a dev build
# - Modify raw package to make it self-hosted
#   - This is referred to as "unsigned" package
# - Ask AMO to sign uBlock0.webext.xpi
#   - Generate JWT to be used for communication with server
#   - Upload unsigned package to AMO
#   - Wait for a valid download URL for signed package
#   - Download signed package as uBlock0.webext.signed.xpi
#     - This is referred to as "signed" package
# - Upload uBlock0.webext.signed.xpi to GitHub
# - Remove uBlock0.webext.xpi from GitHub
# - Modify updates.json to point to new version
#   - Commit changes to repo

# Find path to project root
projdir = os.path.split(os.path.abspath(__file__))[0]
while not os.path.isdir(os.path.join(projdir, '.git')):
    projdir = os.path.normpath(os.path.join(projdir, '..'))

extension_id = 'uBlock0@raymondhill.net'
tmpdir = tempfile.TemporaryDirectory()
raw_xpi_filename = 'uBlock0.webext.xpi'
raw_xpi_filepath = os.path.join(tmpdir.name, raw_xpi_filename)
unsigned_xpi_filepath = os.path.join(tmpdir.name, 'uBlock0.webext.unsigned.xpi')
signed_xpi_filename = 'uBlock0.webext.signed.xpi'
signed_xpi_filepath = os.path.join(tmpdir.name, signed_xpi_filename)
github_owner = 'gorhill'
github_repo = 'uBlock'

# We need a version string to work with
if len(sys.argv) >= 2 and sys.argv[1]:
    version = sys.argv[1]
else:
    version = input('Github release version: ')
version.strip()
if not re.search('^\d+\.\d+\.\d+(b|rc)\d+$', version):
    print('Error: Invalid version string.')
    exit(1)

# GitHub API token
# TODO: support as environment variable? (see os.environ)
github_token = input("Github token: ").strip()
if len(github_token) == 0:
    print('Error: invalid GitHub token')
    exit(1)
github_auth = 'token ' + github_token

#
# Get metadata from GitHub about the release
#

# https://developer.github.com/v3/repos/releases/#get-a-single-release
print('Downloading release info from GitHub...')
release_info_url = 'https://api.github.com/repos/{0}/{1}/releases/tags/{2}'.format(github_owner, github_repo, version)
headers = { 'Authorization': github_auth, }
response = requests.get(release_info_url, headers=headers)
if response.status_code != 200:
    print('Error: Release not found: {0}'.format(response.status_code))
    exit(1)
release_info = response.json()

#
# Extract URL to raw package from metadata
#

# Find url for uBlock0.webext.xpi
raw_xpi_url = ''
for asset in release_info['assets']:
    if asset['name'] == signed_xpi_filename:
        print('Error: Found existing signed self-hosted package.')
        exit(1)
    if asset['name'] == raw_xpi_filename:
        raw_xpi_url = asset['url']
if len(raw_xpi_url) == 0:
    print('Error: Release asset URL not found')
    exit(1)

#
# Download raw package from GitHub
#

# https://developer.github.com/v3/repos/releases/#get-a-single-release-asset
print('Downloading raw xpi package from GitHub...')
headers = {
    'Authorization': github_auth,
    'Accept': 'application/octet-stream',
}
response = requests.get(raw_xpi_url, headers=headers)
# Redirections are transparently handled:
# http://docs.python-requests.org/en/master/user/quickstart/#redirection-and-history
if response.status_code != 200:
    print('Error: Downloading raw package failed -- server error {0}'.format(response.status_code))
    exit(1)
with open(raw_xpi_filepath, 'wb') as f:
    f.write(response.content)
print('Downloaded raw package saved as {0}'.format(raw_xpi_filepath))

#
# Convert the package to a self-hosted one: add `update_url` to the manifest
#

print('Converting raw xpi package into self-hosted xpi package...')
with zipfile.ZipFile(raw_xpi_filepath, 'r') as zipin:
    with zipfile.ZipFile(unsigned_xpi_filepath, 'w') as zipout:
        for item in zipin.infolist():
            data = zipin.read(item.filename)
            if item.filename == 'manifest.json':
                manifest = json.loads(bytes.decode(data))
                manifest['applications']['gecko']['update_url'] = 'https://raw.githubusercontent.com/{0}/{1}/master/dist/firefox/updates.json'.format(github_owner, github_repo)
                data = json.dumps(manifest, indent=2, separators=(',', ': '), sort_keys=True).encode()
            zipout.writestr(item, data)

#
# Ask AMO to sign the self-hosted package
# - https://developer.mozilla.org/en-US/Add-ons/Distribution#Distributing_your_add-on
# - https://pyjwt.readthedocs.io/en/latest/usage.html
# - https://addons-server.readthedocs.io/en/latest/topics/api/auth.html
# - https://addons-server.readthedocs.io/en/latest/topics/api/signing.html
#

print('Ask AMO to sign self-hosted xpi package...')
with open(unsigned_xpi_filepath, 'rb') as f:
    # TODO: support use of env variables for key/secret?
    amo_api_key = input("AMO API key: ").strip()
    amo_secret = input("AMO API secret: ").strip()
    amo_nonce = os.urandom(8).hex()
    jwt_payload = {
        'iss': amo_api_key,
        'jti': amo_nonce,
        'iat': datetime.datetime.utcnow(),
        'exp': datetime.datetime.utcnow() + datetime.timedelta(seconds=180),
    }
    jwt_auth = 'JWT ' + jwt.encode(jwt_payload, amo_secret).decode()
    headers = { 'Authorization': jwt_auth, }
    data = { 'channel': 'unlisted' }
    files = { 'upload': f, }
    signing_url = 'https://addons.mozilla.org/api/v3/addons/{0}/versions/{1}/'.format(extension_id, version)
    print('Submitting package to be signed...')
    response = requests.put(signing_url, headers=headers, data=data, files=files)
    if response.status_code != 202:
        print('Error: Creating new version failed -- server error {0}'.format(response.status_code))
        print(response.text)
        exit(1)
    print('Request for signing self-hosted xpi package succeeded.')
    signing_request_response = response.json();
    f.close()
    print('Waiting for AMO to process the request to sign the self-hosted xpi package...')
    # Wait for signed package to be ready
    signing_check_url = signing_request_response['url']
    # TODO: use real time instead
    countdown = 180 / 5
    while True:
        sys.stdout.write('.')
        sys.stdout.flush()
        time.sleep(5)
        countdown -= 5
        if countdown <= 0:
            print('Error: AMO signing timed out')
            exit(1)
        response = requests.get(signing_check_url, headers=headers)
        if response.status_code != 200:
            print('Error: AMO signing failed -- server error {0}'.format(response.status_code))
            exit(1)
        signing_check_response = response.json()
        if not signing_check_response['processed']:
            continue
        if not signing_check_response['valid']:
            print('Error: AMO validation failed')
            exit(1)
        if not signing_check_response['files'] or len(signing_check_response['files']) == 0:
            continue
        if not signing_check_response['files'][0]['signed']:
            print('Error: AMO signing failed')
            exit(1)
        print('\r')
        print('Self-hosted xpi package successfully signed.')
        download_url = signing_check_response['files'][0]['download_url']
        print('Downloading signed self-hosted xpi package from {0}...'.format(download_url))
        response = requests.get(download_url, headers=headers)
        if response.status_code != 200:
            print('Error: Download signed package failed -- server error {0}'.format(response.status_code))
            exit(1)
        with open(signed_xpi_filepath, 'wb') as f:
            f.write(response.content)
            f.close()
        print('Signed self-hosted xpi package downloaded.')
        break

#
# Upload signed package to GitHub
#

# https://developer.github.com/v3/repos/releases/#upload-a-release-asset
print('Uploading signed self-hosted xpi package to GitHub...')
with open(signed_xpi_filepath, 'rb') as f:
    url = release_info['upload_url'].replace('{?name,label}', '?name=' + signed_xpi_filename)
    headers = {
        'Authorization': github_auth,
        'Content-Type': 'application/zip',
    }
    response = requests.post(url, headers=headers, data=f.read())
    if response.status_code != 201:
        print('Error: Upload signed package failed -- server error: {0}'.format(response.status_code))
        exit(1)

#
# Remove raw package from GitHub
#

# https://developer.github.com/v3/repos/releases/#delete-a-release-asset
print('Remove raw xpi package from GitHub...')
headers = { 'Authorization': github_auth, }
response = requests.delete(raw_xpi_url, headers=headers)
if response.status_code != 204:
    print('Error: Deletion of raw package failed -- server error: {0}'.format(response.status_code))

#
# Update updates.json to point to new package -- but only if just-signed
# package is higher version than current one.
#

print('Update GitHub to point to newly signed self-hosted xpi package...')
updates_json_filepath = os.path.join(projdir, 'dist', 'firefox', 'updates.json')
with open(updates_json_filepath) as f:
    updates_json = json.load(f)
    f.close()
    previous_version = updates_json['addons'][extension_id]['updates'][0]['version']
    if StrictVersion(version) > StrictVersion(previous_version):
        with open(os.path.join(projdir, 'platform', 'webext', 'updates.template.json')) as f:
            template_json = Template(f.read())
            f.close()
            updates_json = template_json.substitute(version=version)
            with open(updates_json_filepath, 'w') as f:
               f.write(updates_json)
               f.close()
        # TODO: automatically git add/commit?

print('All done.')
