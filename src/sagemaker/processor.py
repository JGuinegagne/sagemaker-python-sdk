# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""This module contains code related to the Processor class, which is used
for Processing jobs. These jobs let customers perform data pre-processing,
post-processing, feature engineering, data validation, and model evaluation
and interpretation on SageMaker.
"""
from __future__ import print_function, absolute_import

import os

from six.moves.urllib.parse import urlparse

from six import string_types

from sagemaker.job import _Job
from sagemaker.utils import base_name_from_image, name_from_base
from sagemaker.session import Session
from sagemaker.s3 import (
    S3CompressionType,
    S3DataDistributionType,
    S3DataType,
    S3DownloadMode,
    S3InputMode,
    S3UploadMode,
    S3Uploader,
)


class Processor(object):
    """Handles Amazon SageMaker processing tasks."""

    def __init__(
        self,
        role,
        image_uri,
        processing_instance_count,
        processing_instance_type,
        entrypoint=None,
        arguments=None,
        processing_volume_size_in_gb=30,
        processing_volume_kms_key=None,
        processing_max_runtime_in_seconds=24 * 60 * 60,
        base_job_name=None,
        sagemaker_session=None,
        env=None,
        tags=None,
        network_config=None,
    ):
        """Initialize a ``Processor`` instance. The Processor handles Amazon
        SageMaker processing tasks.

        Args:
            role (str): An AWS IAM role. The Amazon SageMaker training jobs
                and APIs that create Amazon SageMaker endpoints use this role
                to access training data and model artifacts. After the endpoint
                is created, the inference code might use the IAM role, if it
                needs to access an AWS resource.
            image_uri (str): The uri of the image to use for the processing
                jobs started by the Processor.
            processing_instance_count (int): The number of instances to run
                the Processing job with.
            processing_instance_type (str): Type of EC2 instance to use for
                processing, for example, 'ml.c4.xlarge'.
            entrypoint (str): The entrypoint for the processing job.
            arguments ([str]): A list of string arguments to be passed to a
                processing job.
            processing_volume_size_in_gb (int): Size in GB of the EBS volume
                to use for storing data during processing (default: 30).
            processing_volume_kms_key (str): A KMS key for the processing
                volume.
            processing_max_runtime_in_seconds (int): Timeout in seconds
                (default: 24 * 60 * 60). After this amount of time Amazon
                SageMaker terminates the job regardless of its current status.
            base_job_name (str): Prefix for processing name. If not specified,
                the processor generates a default job name, based on the
                training image name and current timestamp.
            sagemaker_session (sagemaker.session.Session): Session object which
                manages interactions with Amazon SageMaker APIs and any other
                AWS services needed. If not specified, the processor creates one
                using the default AWS configuration chain.
            env (dict): Environment variables to be passed to the processing job.
            tags ([dict]): List of tags to be passed to the processing job.
            network_config (sagemaker.network.NetworkConfig): A NetworkConfig
                object that configures network isolation, encryption of
                inter-container traffic, security group IDs, and subnets.
        """
        self.role = role
        self.image_uri = image_uri
        self.processing_instance_count = processing_instance_count
        self.processing_instance_type = processing_instance_type
        self.entrypoint = entrypoint
        self.arguments = arguments
        self.processing_volume_size_in_gb = processing_volume_size_in_gb
        self.processing_volume_kms_key = processing_volume_kms_key
        self.processing_max_runtime_in_seconds = processing_max_runtime_in_seconds
        self.base_job_name = base_job_name
        self.sagemaker_session = sagemaker_session or Session()
        self.env = env
        self.tags = tags
        self.network_config = network_config

        self.jobs = []
        self.latest_job = None
        self._current_job_name = None

    def run(self, inputs=None, outputs=None, wait=True, logs=True, job_name=None):
        """Run a processing job.

        Args:
            inputs ([sagemaker.processor.FileInput]): Input files for the processing
                job. These must be provided as FileInput objects.
            outputs ([str or sagemaker.processor.FileOutput]): Outputs for the processing
                job. These can be specified as either a path string or a FileOutput
                object.
            wait (bool): Whether the call should wait until the job completes (default: True).
            logs (bool): Whether to show the logs produced by the job.
                Only meaningful when wait is True (default: True).
            job_name (str): Processing job name. If not specified, the processor generates
                a default job name, based on the image name and current timestamp.
        """
        self._current_job_name = self._generate_current_job_name(job_name=job_name)

        normalized_inputs = self._normalize_inputs(inputs)
        normalized_outputs = self._normalize_outputs(outputs)

        self.latest_job = ProcessingJob.start_new(self, normalized_inputs, normalized_outputs)
        self.jobs.append(self.latest_job)
        if wait:
            self.latest_job.wait(logs=logs)

    def _generate_current_job_name(self, job_name=None):
        """Generate the job name before running a processing job.

        Args:
            job_name (str): Name of the processing job to be created. If not
                specified, one is generated, using the base name given to the
                constructor if applicable.

        Returns:
            str: The supplied or generated job name.
        """
        if job_name is not None:
            return job_name
        # Honor supplied base_job_name or generate it.
        if self.base_job_name:
            base_name = self.base_job_name
        else:
            base_name = base_name_from_image(self.image_uri)

        return name_from_base(base_name)

    def _normalize_inputs(self, inputs=None):
        """Ensure that all the FileInput objects have names and S3 uris.

        Args:
            inputs ([sagemaker.processor.FileInput]): A list of FileInput
                objects to be normalized.

        Returns:
            [sagemaker.processor.FileInput]: The list of normalized
            FileInput objects.
        """
        # Initialize a list of normalized FileInput objects.
        normalized_inputs = []
        if inputs is not None:
            # Iterate through the provided list of inputs.
            for count, file_input in enumerate(inputs, 1):
                if not isinstance(file_input, FileInput):
                    raise TypeError("Your inputs must be provided as FileInput objects.")
                # Generate a name for the FileInput if it doesn't have one.
                if file_input.input_name is None:
                    file_input.input_name = "input-{}".format(count)
                # If the source is a local path, upload it to S3
                # and save the S3 uri in the FileInput source.
                parse_result = urlparse(file_input.source)
                if parse_result.scheme != "s3":
                    desired_s3_uri = os.path.join(
                        "s3://",
                        self.sagemaker_session.default_bucket(),
                        self._current_job_name,
                        file_input.input_name,
                    )
                    s3_uri = S3Uploader.upload(
                        local_path=file_input.source,
                        desired_s3_uri=desired_s3_uri,
                        session=self.sagemaker_session,
                    )
                    file_input.source = s3_uri
                normalized_inputs.append(file_input)
        return normalized_inputs

    def _normalize_outputs(self, outputs=None):
        """Ensure that all the outputs are FileOutput objects with
        names and S3 uris.

        Args:
            outputs ([str or sagemaker.processor.FileOutput]): A list
                of outputs to be normalized. Can be either strings or
                FileOutput objects.

        Returns:
            [sagemaker.processor.FileOutput]: The list of normalized
                FileOutput objects.
        """
        # Initialize a list of normalized FileOutput objects.
        normalized_outputs = []
        if outputs is not None:
            # Iterate through the provided list of outputs.
            for count, output in enumerate(outputs, 1):
                # If the output is a string, turn it into a FileOutput object.
                if isinstance(output, string_types):
                    s3_uri = os.path.join(
                        "s3://",
                        self.sagemaker_session.default_bucket(),
                        self._current_job_name,
                        "output",
                    )
                    output = FileOutput(source=output, destination=s3_uri)
                # Generate a name for the FileOutput if it doesn't have one.
                if output.output_name is None:
                    output.output_name = "output-{}".format(count)
                normalized_outputs.append(output)
        return normalized_outputs

    def attach(self, processing_job_name, sagemaker_session=None):
        """Attach to an existing processing job.

        Args:
            processing_job_name (str): The name of the processing job to attach to.
            sagemaker_session (sagemaker.session.Session): Session object which
                manages interactions with Amazon SageMaker APIs and any other
                AWS services needed. If not specified, one is created using the
                default AWS configuration chain.

        Returns:
            sagemaker.processor.Processor: The Processor instance with the
                specified processing job attached.
        """
        raise NotImplementedError


class ScriptModeProcessor(Processor):
    """Handles Amazon SageMaker processing tasks for jobs using a machine learning framework."""

    def __init__(
        self,
        role,
        image_uri,
        processing_instance_count,
        processing_instance_type,
        py_version="py3",
        arguments=None,
        processing_volume_size_in_gb=30,
        processing_volume_kms_key=None,
        processing_max_runtime_in_seconds=24 * 60 * 60,
        base_job_name=None,
        sagemaker_session=None,
        env=None,
        tags=None,
        network_config=None,
    ):
        """Initialize a ``ScriptModeProcessor`` instance. The ScriptModeProcessor
        handles Amazon SageMaker processing tasks for jobs using script mode.

        Args:
            role (str): An AWS IAM role. The Amazon SageMaker training jobs
                and APIs that create Amazon SageMaker endpoints use this role
                to access training data and model artifacts. After the endpoint
                is created, the inference code might use the IAM role, if it
                needs to access an AWS resource.
            image_uri (str): The uri of the image to use for the processing
                jobs started by the Processor.
            processing_instance_count (int): The number of instances to run
                the Processing job with.
            processing_instance_type (str): Type of EC2 instance to use for
                processing, for example, 'ml.c4.xlarge'.
            py_version (str): The python version to use, for example, 'py3'.
            arguments ([str]): A list of string arguments to be passed to a
                processing job.
            processing_volume_size_in_gb (int): Size in GB of the EBS volume
                to use for storing data during processing (default: 30).
            processing_volume_kms_key (str): A KMS key for the processing
                volume.
            processing_max_runtime_in_seconds (int): Timeout in seconds
                (default: 24 * 60 * 60). After this amount of time Amazon
                SageMaker terminates the job regardless of its current status.
            base_job_name (str): Prefix for processing name. If not specified,
                the processor generates a default job name, based on the
                training image name and current timestamp.
            sagemaker_session (sagemaker.session.Session): Session object which
                manages interactions with Amazon SageMaker APIs and any other
                AWS services needed. If not specified, the processor creates one
                using the default AWS configuration chain.
            env (dict): Environment variables to be passed to the processing job.
            tags ([dict]): List of tags to be passed to the processing job.
            network_config (sagemaker.network.NetworkConfig): A NetworkConfig
                object that configures network isolation, encryption of
                inter-container traffic, security group IDs, and subnets.
        """
        self.py_version = py_version
        self.CODE_CONTAINER_BASE_PATH = "/code/"
        self.CODE_CONTAINER_INPUT_NAME = "source"

        super(ScriptModeProcessor, self).__init__(
            role=role,
            image_uri=image_uri,
            processing_instance_count=processing_instance_count,
            processing_instance_type=processing_instance_type,
            arguments=arguments,
            processing_volume_size_in_gb=processing_volume_size_in_gb,
            processing_volume_kms_key=processing_volume_kms_key,
            processing_max_runtime_in_seconds=processing_max_runtime_in_seconds,
            base_job_name=base_job_name,
            sagemaker_session=sagemaker_session,
            env=env,
            tags=tags,
            network_config=network_config,
        )

    def run(
        self,
        source,
        script_name=None,
        inputs=None,
        outputs=None,
        wait=True,
        logs=True,
        job_name=None,
    ):
        """Run a processing job with Script Mode.

        Args:
            source (str): This can be an S3 uri or a local path to either
                a directory or a file with the user's script to run.
            script_name (str): If the user provides a directory for source,
                they must specify script_name as the file within that
                directory to use.
            inputs ([sagemaker.processor.FileInput]): Input files for the processing
                job. These must be provided as FileInput objects.
            outputs ([str or sagemaker.processor.FileOutput]): Outputs for the processing
                job. These can be specified as either a path string or a FileOutput
                object.
            wait (bool): Whether the call should wait until the job completes (default: True).
            logs (bool): Whether to show the logs produced by the job.
                Only meaningful when wait is True (default: True).
            job_name (str): Processing job name. If not specified, the processor generates
                a default job name, based on the image name and current timestamp.
        """
        self._current_job_name = self._generate_current_job_name(job_name=job_name)

        customer_script_name = self._get_customer_script_name(source, script_name)
        customer_code_s3_uri = self._upload_source(source)
        inputs_with_source = self._convert_source_and_add_to_inputs(inputs, customer_code_s3_uri)

        self._set_entrypoint(customer_script_name)

        super(ScriptModeProcessor, self).run(
            inputs=inputs_with_source, outputs=outputs, wait=wait, logs=logs, job_name=job_name
        )

    def _get_customer_script_name(self, source, script_name):
        """Finds the customer script name using the provided source file,
        directory, or script name.

        Args:
            source (str): This can be an S3 uri or a local path to either
                a directory or a file.
            script_name (str): If the user provides a directory as source,
                they must specify script_name as the file within that
                directory to use.

        Returns:
            str: The script name from the S3 uri or from the file found
                on the user's local machine.
        """
        parse_result = urlparse(source)

        if os.path.isdir(source) and script_name is None:
            raise ValueError(
                """You provided a directory without providing a script name.
                Please provide a script name inside the directory that you specified.
                """
            )
        if (parse_result.scheme == "s3" or os.path.isdir(source)) and script_name is not None:
            return script_name
        if parse_result.scheme == "s3" or os.path.isfile(source):
            return os.path.basename(source)
        raise ValueError("The file or directory you specified does not exist.")

    def _upload_source(self, source):
        """Uploads a source file or directory specified as a string
        and returns the S3 uri.

        Args:
            source (str): A file or directory to be uploaded to S3.

        Returns:
            str: The S3 uri of the uploaded file or directory.

        """
        desired_s3_uri = os.path.join(
            "s3://",
            self.sagemaker_session.default_bucket(),
            self._current_job_name,
            self.CODE_CONTAINER_INPUT_NAME,
        )
        return S3Uploader.upload(
            local_path=source, desired_s3_uri=desired_s3_uri, session=self.sagemaker_session
        )

    def _convert_source_and_add_to_inputs(self, inputs, s3_uri):
        """Creates a FileInput object from an S3 uri and adds it to the list of inputs.

        Args:
            inputs ([sagemaker.processor.FileInput]): List of FileInput objects.
            s3_uri (str): S3 uri of the input to be added to inputs.

        Returns:
            [sagemaker.processor.FileInput]: A new list of FileInput objects, with
                the FileInput object created from s3_uri appended to the list.

        """
        input_list = inputs
        source_file_input = FileInput(
            source=s3_uri,
            destination=os.path.join(self.CODE_CONTAINER_BASE_PATH, self.CODE_CONTAINER_INPUT_NAME),
            input_name=self.CODE_CONTAINER_INPUT_NAME,
        )
        input_list.append(source_file_input)
        return input_list

    def _get_execution_program(self, script_name):
        """Determine which executable to run the user's script with
        based on the file extension.

        Args:
            script_name (str): A filename with an extension.

        Returns:
            str: A name of an executable to run the user's script with.
        """
        file_extension = os.path.splitext(script_name)[1]
        if file_extension == ".py":
            if self.py_version == "py3":
                return "python3"
            if self.py_version == "py2":
                return "python2"
            return "python"
        if file_extension == ".sh":
            return "bash"
        raise ValueError(
            """Script Mode supports Python or Bash scripts.
            To use a custom entrypoint, please use Processor.
            """
        )

    def _set_entrypoint(self, customer_script_name):
        """Sets the entrypoint based on the customer's script and corresponding executable.

        Args:
            customer_script_name (str): A filename with an extension.
        """
        customer_script_location = os.path.join(
            self.CODE_CONTAINER_BASE_PATH, self.CODE_CONTAINER_INPUT_NAME, customer_script_name
        )
        execution_program = self._get_execution_program(customer_script_name)
        self.entrypoint = [execution_program, customer_script_location]


class ProcessingJob(_Job):
    """Provides functionality to start, describe, and stop processing jobs."""

    def __init__(self, sagemaker_session, job_name, inputs, outputs):
        self.inputs = inputs
        self.outputs = outputs
        super(ProcessingJob, self).__init__(sagemaker_session=sagemaker_session, job_name=job_name)

    @classmethod
    def start_new(cls, processor, inputs, outputs):
        """Start a new processing job using the provided inputs and outputs.

        Args:
            processor (sagemaker.processor.Processor): The Processor instance
                that started the job.
            inputs ([sagemaker.processor.FileInput]): A list of FileInput objects.
            outputs ([sagemaker.processor.FileOutput]): A list of FileOutput objects.

        Returns:
            sagemaker.processor.ProcessingJob: The instance of ProcessingJob created
                using the current job name.

        """
        # Initialize an empty dictionary for arguments to be passed to sagemaker_session.process.
        process_request_args = {}

        # Add arguments to the dictionary.
        process_request_args["inputs"] = [input.to_request_dict() for input in inputs]
        process_request_args["outputs"] = [output.to_request_dict() for output in outputs]
        process_request_args["job_name"] = processor._current_job_name
        process_request_args["resources"] = {
            "ClusterConfig": {
                "InstanceType": processor.processing_instance_type,
                "InstanceCount": processor.processing_instance_count,
                "VolumeSizeInGB": processor.processing_volume_size_in_gb,
            }
        }
        process_request_args["stopping_condition"] = {
            "MaxRuntimeInSeconds": processor.processing_max_runtime_in_seconds
        }
        process_request_args["app_specification"] = {"ImageUri": processor.image_uri}
        if processor.arguments is not None:
            process_request_args["app_specification"]["ContainerArguments"] = processor.arguments
        if processor.entrypoint is not None:
            process_request_args["app_specification"]["ContainerEntrypoint"] = processor.entrypoint
        process_request_args["environment"] = processor.env
        if processor.network_config is not None:
            process_request_args["network_config"] = processor.network_config.to_request_dict()
        else:
            process_request_args["network_config"] = None
        process_request_args["role_arn"] = processor.role
        process_request_args["tags"] = processor.tags

        # Print the job name and the user's inputs and outputs as lists of dictionaries.
        print("Job Name: ", process_request_args["job_name"])
        print("Inputs: ", process_request_args["inputs"])
        print("Outputs: ", process_request_args["outputs"])

        # Call sagemaker_session.process using the arguments dictionary.
        processor.sagemaker_session.process(**process_request_args)

        return cls(processor.sagemaker_session, processor._current_job_name, inputs, outputs)

    def _is_local_channel(self, input_url):
        """Used for Local Mode. Not yet implemented.
        Args:
            input_url (str):
        """
        raise NotImplementedError

    def wait(self, logs=True):
        if logs:
            self.sagemaker_session.logs_for_processing_job(self.job_name, wait=True)
        else:
            self.sagemaker_session.wait_for_processing_job(self.job_name)

    def describe(self, print_response=True):
        """Prints out a response from the DescribeProcessingJob API call."""
        describe_response = self.sagemaker_session.describe_analytics_job(self.job_name)
        if print_response:
            print(describe_response)
        return describe_response

    def stop(self):
        """Stops the processing job."""
        self.sagemaker_session.stop_processing_job(self.name)


class FileInput(object):
    """Accepts parameters that specify an S3 input for a processing job and provides
    a method to turn those parameters into a dictionary."""

    def __init__(
        self,
        source,
        destination,
        input_name=None,
        s3_data_type=S3DataType.MANIFEST_FILE,
        s3_input_mode=S3InputMode.FILE,
        s3_download_mode=S3DownloadMode.CONTINUOUS,
        s3_data_distribution_type=S3DataDistributionType.FULLY_REPLICATED,
        s3_compression_type=S3CompressionType.NONE,
    ):
        """Initialize a ``FileInput`` instance. FileInput accepts parameters
        that specify an S3 input for a processing job and provides a method
        to turn those parameters into a dictionary.

        Args:
            source (str): The source for the input.
            destination (str): The destination of the input.
            input_name (str): The user-provided name for the input. If a name
                is not provided, one will be generated.
            s3_data_type (sagemaker.s3.S3DataType):
            s3_input_mode (sagemaker.s3.S3InputMode):
            s3_download_mode (sagemaker.s3.S3DownloadMode):
            s3_data_distribution_type (sagemaker.s3.S3DataDistributionType):
            s3_compression_type (sagemaker.s3.S3CompressionType):
        """
        self.source = source
        self.destination = destination
        self.input_name = input_name
        self.s3_data_type = s3_data_type
        self.s3_input_mode = s3_input_mode
        self.s3_download_mode = s3_download_mode
        self.s3_data_distribution_type = s3_data_distribution_type
        self.s3_compression_type = s3_compression_type

    def to_request_dict(self):
        """Generates a request dictionary using the parameters provided to the class."""
        # Create the request dictionary.
        s3_input_request = {
            "InputName": self.input_name,
            "S3Input": {
                "S3Uri": self.source,
                "LocalPath": self.destination,
                "S3DataType": self.s3_data_type.value,
                "S3InputMode": self.s3_input_mode.value,
                "S3DownloadMode": self.s3_download_mode.value,
                "S3DataDistributionType": self.s3_data_distribution_type.value,
            },
        }

        # Check the compression type, then add it to the dictionary.
        if (
            self.s3_compression_type == S3CompressionType.GZIP
            and self.s3_input_mode != S3InputMode.PIPE
        ):
            raise ValueError("Data can only be gzipped when the input mode is Pipe.")
        if self.s3_compression_type is not None:
            s3_input_request["S3Input"]["S3CompressionType"] = self.s3_compression_type.value

        # Return the request dictionary.
        return s3_input_request


class FileOutput(object):
    """Accepts parameters that specify an S3 output for a processing job and provides
    a method to turn those parameters into a dictionary."""

    def __init__(
        self,
        source,
        destination,
        output_name=None,
        kms_key_id=None,
        s3_upload_mode=S3UploadMode.CONTINUOUS,
    ):
        """Initialize a ``FileOutput`` instance. FileOutput accepts parameters that
        specify an S3 output for a processing job and provides a method to turn
        those parameters into a dictionary.

        Args:
            source (str): The source for the output.
            destination (str): The destination of the output.
            output_name (str): The name of the output.
            kms_key_id (str): The KMS key id for the output.
            s3_upload_mode (sagemaker.s3.S3UploadMode):
        """
        self.source = source
        self.destination = destination
        self.output_name = output_name
        self.kms_key_id = kms_key_id
        self.s3_upload_mode = s3_upload_mode

    def to_request_dict(self):
        """Generates a request dictionary using the parameters provided to the class."""
        # Create the request dictionary.
        s3_output_request = {
            "OutputName": self.output_name,
            "S3Output": {
                "S3Uri": self.destination,
                "LocalPath": self.source,
                "S3UploadMode": self.s3_upload_mode.value,
            },
        }

        # Check the KMS key ID, then add it to the dictionary.
        if self.kms_key_id is not None:
            s3_output_request["S3Output"]["KmsKeyId"] = self.kms_key_id

        # Return the request dictionary.
        return s3_output_request
