import glob
import io
import json
import os
import re
import pkg_resources
import requests
import stat
import subprocess
import sys
import time
import csv

from builtins import input
from future.utils import viewitems

sys.tracebacklimit = 0

DEFAULT_MAX_PART_SIZE_IN_MB = 5000
INPUT_REGEX = "(.+)\.(fastq|fq|fasta|fa)(\.gz|$)"
PAIRED_REGEX = "(.+)(_R\d)(_001)?\.(fastq|fq|fasta|fa)(\.gz|$)"
PART_SUFFIX = "__AWS-MULTI-PART-"


class File():
    def __init__(self, path):
        self.path = path

    def source_type(self):
        if self.path.startswith('s3://'):
            return 's3'
        elif stat.S_ISREG(os.stat(self.path).st_mode):
            return 'local'

    def parts(self, max_part_size):
        # Check if any file is over max_part_size and, if so, chunk
        if self.source_type() == 'local' and os.path.getsize(
                self.path) > max_part_size * 1048576:
            part_prefix = self.path + PART_SUFFIX
            print("splitting large file into {} MB chunks...".format(
                max_part_size))
            subprocess.check_output(
                "split -b {}m {} {}".format(max_part_size, self.path,
                                            part_prefix),
                shell=True)
            return subprocess.check_output(
                "ls {}*".format(part_prefix), shell=True).decode("utf-8").splitlines()
        else:
            return [self.path]


def build_path(bucket, key):
    return "s3://{}/{}".format(bucket, key)


def determine_level(file_path, search_key):
    n_parts_file = len(file_path.split("/"))
    n_parts_key = len(search_key.rstrip("/").split("/"))
    return n_parts_file - n_parts_key


def detect_files(path, level=1):
    # S3 source (user needs access to the location they're trying to upload from):
    if path.startswith('s3://'):
        clean_path = path.rstrip('/')
        bucket = path.split("/")[2]
        file_list = subprocess.check_output(
            "aws s3 ls {}/ --recursive | awk '{{print $4}}'".format(clean_path),
            shell=True).splitlines()
        file_list = [f.decode("UTF-8") for f in file_list]
        return [
            build_path(bucket, f)
            for f in file_list
            if re.search(INPUT_REGEX, f) and determine_level(build_path(bucket, f), clean_path) == level
        ]
    # local source:
    wildcards = "/*" * level
    return [
        f for f in glob.glob(path + wildcards)
        if re.search(INPUT_REGEX, f) and os.stat(f).st_size > 0
    ]


def clean_samples2files(samples2files):
    # Sort files (R1 before R2) and remove samples that don't have 1 or 2 files:
    return {
        k: sorted(v)
        for k, v in viewitems(samples2files) if len(v) in [1, 2]
    }


def detect_samples(path):
    samples2files = {}
    # First try to find top-level files in the folder.
    # Paired files for the same sample must be labeled with R1 and R2 as indicated in PAIRED_REGEX
    files_level1 = detect_files(path, level=1)
    if files_level1:
        for f in files_level1:
            m2 = re.search(PAIRED_REGEX, f)
            m = re.search(INPUT_REGEX, f)
            sample_name = os.path.basename(
                m2.group(1)) if m2 else os.path.basename(m.group(1))
            samples2files[sample_name] = samples2files.get(sample_name,
                                                           []) + [f]
        return clean_samples2files(samples2files)
    # If there are no top-level files, try to find them in subfolders.
    # In this case, each subfolder corresponds to one sample.
    files_level2 = detect_files(path, level=2)
    if files_level2:
        for f in files_level2:
            sample_name = os.path.basename(os.path.dirname(f))
            samples2files[sample_name] = samples2files.get(sample_name,
                                                           []) + [f]
        return clean_samples2files(samples2files)
    # If there are still no suitable files, tell the user hopw folders must be structured.
    print(
        "\n\nNo fastq/fasta files found in this folder.\n"
        "Files can have extensions fastq/fq/fasta/fa "
        "with optionally the additional extension gz.\n"
        "If the folder you specified has no sub-directories, "
        "paired files need to be indicated using the labels _R1 and _R2 before the "
        "extension, otherwise each file will be treated as a separate sample. Sample names "
        "will be derived from file names with the extensions and any R1/R2 labels trimmed off.\n"
        "Alternatively, your folder can be structured to have one subfolder per sample. "
        "In that case, the name of the subfolder will be used as the sample name.\n"
        "Example names: RR004_water_2_S23_R1_001.fastq.gz and RR004_water_2_S23_R2_001.fastq.gz"
    )
    raise ValueError()


def upload(sample_name, project_id, headers, url, r1, r2, host_genome_name, chunk_size, metadata_file):
    print("\nPreparing to uploading sample \"{}\" ...".format(sample_name))

    files = [File(r1)]
    if r2:
        files.append(File(r2))

    source_type = files[0].source_type()

    # Raise exception if a file is empty
    if source_type == 'local' and any(
            os.stat(f.path).st_size == 0 for f in files):
        print("ERROR: input file must not be empty")
        raise ValueError()

    if r2 and files[0].source_type() != files[1].source_type():
        print("ERROR: input files must be same type")
        raise ValueError()

    # Clamp max_part_size to a valid value
    max_part_size = max(min(DEFAULT_MAX_PART_SIZE_IN_MB, chunk_size), 1)

    # Get version of CLI from setuptools
    version = pkg_resources.require("idseq")[0].version

    csv_data = {}
    with open(metadata_file) as f:
        for row in list(csv.DictReader(f)):
            name = row.pop("sample_name")
            csv_data[name] = row

    print("CSV data:")
    print(csv_data)

    data = {
        "samples": [
            {
                "name": sample_name,
                "project_id": project_id,
                "input_files_attributes": [
                    {
                        "name": os.path.basename(f.path),
                        "source": f.path,
                        "source_type": f.source_type(),
                        "parts": ", ".join(f.parts(max_part_size)),
                    }
                    for f in files
                ],
                "host_genome_name": host_genome_name,
                "status": "created"
            }
        ],
        "metadata": csv_data,
        "client": version
    }

    print(f"here's our data: {data}")
    resp = requests.post(
        url + '/samples/bulk_upload_with_metadata.json', data=json.dumps(data), headers=headers)

    if resp.status_code in range(200, 300):
        print("Connected to the server.")
    else:
        print('\nFailed. Error no: {}'.format(resp.status_code))
        print(resp.text)
        print(resp.json())
        for err_type, errors in viewitems(resp.json()):
            print(
                'Error response from IDseq server :: {0} :: {1}'.format(err_type,
                                                                        errors))
        return

    if source_type == 'local':
        data = resp.json()
        print(data)

        sample_data = data["samples"][0]
        num_files = len(sample_data["input_files"])
        if num_files == 1:
            msg = "1 file to upload..."
        else:
            msg = "{} files to upload...".format(num_files)
        print(msg)
        time.sleep(1)

        for raw_input_file in sample_data['input_files']:
            presigned_urls = raw_input_file['presigned_url'].split(", ")
            input_parts = raw_input_file["parts"].split(", ")
            for i, file in enumerate(input_parts):
                presigned_url = presigned_urls[i]
                with Tqio(file, i, num_files) as f:
                    requests.put(presigned_url, data=f)
                if PART_SUFFIX in file:
                    subprocess.check_output("rm {}".format(file), shell=True)

        sample_id = data["sample_ids"][0]
        update = {
            "sample": {
                "id": sample_id,
                "name": sample_name,
                "status": "uploaded"
            }
        }

        resp = requests.put(
            '{}/samples/{}.json'.format(url, sample_id),
            data=json.dumps(update),
            headers=headers)

        print("this happened after the upload")

        if resp.status_code != 200:
            print("Sample was not successfully uploaded. Status code: {}".format(str(
                resp.status_code)))


def get_user_agreement():
    def prompt(msg):
        resp = input(msg)
        if resp.lower() not in ["y", "yes"]:
            print("Exiting...")
            quit()

    msg = "\nConfirm details above.\nProceed (y/N)? y for yes or N to cancel: "
    prompt(msg)
    msg = "\nI agree that the data I am uploading to IDseq has been lawfully " \
          "collected and that I have all the necessary consents, permissions, " \
          "and authorizations needed to collect, share, and export data to " \
          "IDseq as outlined in the Terms (https://assets.idseq.net/Terms.pdf) and Data " \
          "Privacy Notice (https://assets.idseq.net/Privacy.pdf).\nProceed (y/N)? y for " \
          "yes or N to cancel: "
    prompt(msg)


def get_user_metadata(base_url, headers, sample_names):
    print(
        "\n\nPlease provide some metadata for your sample(s):"
        "\n\nInstructions: https://idseq.net/metadata/instructions"
        "\nMetadata dictionary: https://idseq.net/metadata/dictionary"
        "\nMetadata CSV template: https://idseq.net/metadata/metadata_template_csv"
    )
    metadata_file = input("\nEnter the metadata file: ")

    # Loop for metadata CSV validation
    errors = [-1]
    while len(errors) != 0:
        try:
            with open(metadata_file) as f:
                csv_data = list(csv.reader(f))

            # Format data for the validation endpoint
            data = {
                "metadata": {"headers": csv_data[0], "rows": csv_data[1:]},
                "samples": [
                    {"name": name} for name in sample_names
                ],
            }
            resp = requests.post(
                base_url + "/metadata/validate_csv_for_new_samples.json",
                data=json.dumps(data),
                headers=headers,
                )

            # Handle errors
            resp = json.loads(resp.text)
            errors = resp.get("issues", {}).get("errors", {})
        except (OSError, json.decoder.JSONDecodeError, requests.exceptions.RequestException) as err:
            errors = [str(err)]

        if len(errors) == 0:
            print("\nCSV validation successful!")
            return metadata_file
        else:
            print("\n".join(errors))
            resp = input("\nFix these errors and press Enter to upload again. Or enter a different "
                         "file name: ")
            metadata_file = resp or metadata_file


def validate_project(base_url, headers, project_name):
    print("Checking project name...")
    all_projects = requests.get(base_url + "/projects.json", headers=headers).json()
    names_to_ids = {}
    for project in all_projects:
        names_to_ids[project["name"]] = project["id"]

    while project_name not in names_to_ids:
        user_resp = input("\nProject does not exist. Press Enter to create. Or check a different project "
                     "name: ")
        if user_resp:
            project_name = user_resp
        else:
            resp = requests.post(base_url + "/projects.json", data=json.dumps({"project": {"name": project_name}}), headers=headers)
            resp = resp.json()
            return resp["name"], resp["id"]
    return project_name, names_to_ids[project_name]


class Tqio(io.BufferedReader):
    def __init__(self, file_path, i, count):
        super(Tqio, self).__init__(io.open(file_path, "rb"))
        self.write_stdout("\nUploading {}...\n\r".format(file_path))
        self.progress = 0
        self.chunk_idx = 0
        self.total = os.path.getsize(file_path)
        self.done = False

    def write_stdout(self, msg):
        sys.stdout.write(msg)
        sys.stdout.flush()

    def write_percent_stdout(self, percentage):
        self.write_stdout("{:3.1f} % \r".format(percentage))

    def update(self, len_chunk):
        self.progress += len_chunk
        self.chunk_idx += 1
        if self.chunk_idx % 500 == 0:
            # don't slow the upload process down too much
            self.write_percent_stdout((100.0 * self.progress) / self.total)
        if self.progress >= self.total and not self.done:
            self.write_percent_stdout(100.0)
            self.write_stdout("\nDone.\n")
            self.done = True

    def read(self, *args, **kwargs):
        chunk = super(Tqio, self).read(*args, **kwargs)
        self.update(len(chunk))
        return chunk
