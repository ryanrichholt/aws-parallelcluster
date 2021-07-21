# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
# See the License for the specific language governing permissions and limitations under the License.


import logging

import boto3
import pytest
from assertpy import assert_that, soft_assertions
from botocore.config import Config
from pcluster_client import ApiException
from pcluster_client.api import cluster_operations_api, image_operations_api
from pcluster_client.exceptions import NotFoundException
from pcluster_client.model.build_image_request_content import BuildImageRequestContent
from pcluster_client.model.cloud_formation_stack_status import CloudFormationStackStatus
from pcluster_client.model.cluster_status import ClusterStatus
from pcluster_client.model.create_cluster_request_content import CreateClusterRequestContent
from pcluster_client.model.image_build_status import ImageBuildStatus
from pcluster_client.model.image_status_filtering_option import ImageStatusFilteringOption
from pcluster_client.model.update_cluster_request_content import UpdateClusterRequestContent
from utils import generate_stack_name

from tests.common.utils import retrieve_latest_ami

LOGGER = logging.getLogger(__name__)


def _cloudformation_wait(region, stack_name, status):
    config = Config(region_name=region)
    cloud_formation = boto3.client("cloudformation", config=config)
    waiter = cloud_formation.get_waiter(status)
    waiter.wait(StackName=stack_name)


@pytest.mark.usefixtures("os", "instance")
def test_cluster_slurm(request, scheduler, region, pcluster_config_reader, api_client):
    assert_that(scheduler).is_equal_to("slurm")
    initial_config_file = pcluster_config_reader()
    updated_config_file = pcluster_config_reader("pcluster.config.update.yaml")
    with soft_assertions():
        _test_cluster_workflow(request, region, initial_config_file, updated_config_file, api_client)


@pytest.mark.usefixtures("os", "instance")
def test_cluster_awsbatch(request, scheduler, region, pcluster_config_reader, api_client):
    assert_that(scheduler).is_equal_to("awsbatch")
    initial_config_file = pcluster_config_reader()
    updated_config_file = pcluster_config_reader("pcluster.config.update.yaml")
    with soft_assertions():
        _test_cluster_workflow(request, region, initial_config_file, updated_config_file, api_client)


def _test_cluster_workflow(request, region, initial_config_file, updated_config_file, api_client):
    # Create cluster with initial configuration
    with open(initial_config_file) as config_file:
        cluster_config = config_file.read()

    stack_name = generate_stack_name("integ-tests", request.config.getoption("stackname_suffix"))
    cluster_operations_client = cluster_operations_api.ClusterOperationsApi(api_client)

    resp = _test_create_cluster(cluster_operations_client, cluster_config, region, stack_name)
    cluster = resp["cluster"]
    cluster_name = cluster["cluster_name"]
    assert_that(cluster_name).is_equal_to(stack_name)

    _test_list_clusters(cluster_operations_client, cluster, region, "CREATE_IN_PROGRESS")
    _test_describe_cluster(cluster_operations_client, cluster, region, "CREATE_IN_PROGRESS")

    _cloudformation_wait(region, stack_name, "stack_create_complete")

    _test_list_clusters(cluster_operations_client, cluster, region, "CREATE_COMPLETE")
    _test_describe_cluster(cluster_operations_client, cluster, region, "CREATE_COMPLETE")

    # Update cluster with new configuration
    with open(updated_config_file) as config_file:
        updated_cluster_config = config_file.read()
    _test_update_cluster_dryrun(cluster_operations_client, updated_cluster_config, region, stack_name)

    _test_delete_cluster(cluster_operations_client, cluster, region)


def _test_list_clusters(client, cluster, region, status):
    cluster_name = cluster["cluster_name"]

    response = client.list_clusters(region=region)
    target_cluster = _get_cluster(response["items"], cluster_name)
    next_token = response.get("next_token", None)

    while next_token and not target_cluster:
        response = client.list_clusters(region=region, next_token=next_token)
        target_cluster = _get_cluster(response["items"], cluster_name)
        next_token = response.get("next_token", None)

    assert_that(target_cluster).is_not_none()
    assert_that(target_cluster.cluster_name).is_equal_to(cluster_name)
    assert_that(target_cluster.cluster_status).is_equal_to(ClusterStatus(status))
    assert_that(target_cluster.cloudformation_stack_status).is_equal_to(CloudFormationStackStatus(status))


def _get_cluster(clusters, cluster_name):
    for cluster in clusters:
        if cluster["cluster_name"] == cluster_name:
            return cluster
    return None


def _test_describe_cluster(client, cluster, region, status):
    cluster_name = cluster["cluster_name"]
    response = client.describe_cluster(cluster_name, region=region)
    assert_that(response.cluster_name).is_equal_to(cluster_name)
    assert_that(response.cluster_status).is_equal_to(ClusterStatus(status))
    assert_that(response.cloud_formation_status).is_equal_to(CloudFormationStackStatus(status))


def _test_create_cluster(client, cluster_config, region, stack_name):
    body = CreateClusterRequestContent(stack_name, cluster_config)
    return client.create_cluster(body, region=region)


def _test_update_cluster_dryrun(client, cluster_config, region, stack_name):
    body = UpdateClusterRequestContent(cluster_config)
    error_message = "Request would have succeeded, but DryRun flag is set."
    with pytest.raises(ApiException, match=error_message):
        client.update_cluster(stack_name, body, region=region, dryrun=True)


def _test_delete_cluster(client, cluster, region):
    cluster_name = cluster["cluster_name"]
    client.delete_cluster(cluster_name, region=region)

    _cloudformation_wait(region, cluster_name, "stack_delete_complete")

    response = client.list_clusters(region=region)
    cluster_names = [c["cluster_name"] for c in response["items"]]
    assert_that(cluster_names).does_not_contain(cluster_name)


def test_official_images(region, api_client):
    client = image_operations_api.ImageOperationsApi(api_client)
    response = client.describe_official_images(region=region)
    assert_that(response.items).is_not_empty()


@pytest.mark.usefixtures("instance")
def test_custom_image(request, region, os, pcluster_config_reader, api_client):
    base_ami = retrieve_latest_ami(region, os)

    config_file = pcluster_config_reader(config_file="image.config.yaml", parent_image=base_ami)
    with open(config_file) as config_file:
        config = config_file.read()

    image_id = generate_stack_name("integ-tests-build-image", request.config.getoption("stackname_suffix"))
    client = image_operations_api.ImageOperationsApi(api_client)

    _test_build_image(client, image_id, region, config)

    _test_describe_image(client, image_id, region, "BUILD_IN_PROGRESS")
    _test_list_images(client, image_id, region, "PENDING")

    # CFN stack is deleted as soon as image is available
    _cloudformation_wait(region, image_id, "stack_delete_complete")

    _test_describe_image(client, image_id, region, "BUILD_COMPLETE")
    _test_list_images(client, image_id, region, "AVAILABLE")

    _delete_image(client, image_id, region)


def _test_build_image(client, image_id, region, config):
    image_config_data = base64.b64encode(config.encode("utf-8")).decode("utf-8")
    body = BuildImageRequestContent(image_config_data, image_id, region=region)
    response = client.build_image(body)
    LOGGER.info("Build image response: %s", response)
    assert_that(response.image.image_id).is_equal_to(image_id)


def _test_describe_image(client, image_id, region, status):
    response = client.describe_image(image_id, region=region)
    LOGGER.info("Describe image response: %s", response)
    assert_that(response.image_id).is_equal_to(image_id)
    assert_that(response.image_build_status).is_equal_to(ImageBuildStatus(status))


def _test_list_images(client, image_id, region, status):
    response = client.list_images(image_status=ImageStatusFilteringOption(status), region=region)
    target_image = _get_image(response.items, image_id)

    while "next_token" in response and not target_image:
        response = client.list_images(
            image_status=ImageStatusFilteringOption(status), region=region, next_token=response.next_token
        )
        target_image = _get_image(response.items, image_id)

    LOGGER.info("Target image in ListImages response is: %s", target_image)

    assert_that(target_image).is_not_none()


def _get_image(images, image_id):
    for image in images:
        if image.image_id == image_id:
            return image
    return None


def _delete_image(client, image_id, region):
    client.delete_image(image_id, region=region)

    error_message = f"No image or stack associated to parallelcluster image id {image_id}."
    with pytest.raises(NotFoundException, match=error_message):
        client.describe_image(image_id, region=region)