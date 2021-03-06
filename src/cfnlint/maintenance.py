"""
Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
import fnmatch
import json
import logging
import multiprocessing
import os
import jsonpointer
import jsonpatch
import cfnlint
from cfnlint.helpers import get_url_content, url_has_newer_version
from cfnlint.helpers import SPEC_REGIONS
import cfnlint.data.ExtendedSpecs
import cfnlint.data.AdditionalSpecs


LOGGER = logging.getLogger(__name__)


def update_resource_specs():
    """ Update Resource Specs """

    # Pool() uses cpu count if no number of processors is specified
    # Pool() only implements the Context Manager protocol from Python3.3 onwards,
    # so it will fail Python2.7 style linting, as well as throw AttributeError
    try:
        # pylint: disable=not-context-manager
        with multiprocessing.Pool() as pool:
            pool.starmap(update_resource_spec, SPEC_REGIONS.items())
    except AttributeError:

        # Do it the long, slow way
        for region, url in SPEC_REGIONS.items():
            update_resource_spec(region, url)

def update_resource_spec(region, url):
    """ Update a single resource spec """
    filename = os.path.join(os.path.dirname(cfnlint.__file__), 'data/CloudSpecs/%s.json' % region)

    multiprocessing_logger = multiprocessing.log_to_stderr()

    multiprocessing_logger.debug('Downloading template %s into %s', url, filename)

    # Check to see if we already have the latest version, and if so stop
    if not url_has_newer_version(url):
        return

    spec_content = get_url_content(url, caching=True)

    multiprocessing_logger.debug('A more recent version of %s was found, and will be downloaded to %s', url, filename)
    spec = json.loads(spec_content)

    # Patch the files
    spec = patch_spec(spec, 'all')
    spec = patch_spec(spec, region)

    with open(filename, 'w') as f:
        json.dump(spec, f, indent=2, sort_keys=True, separators=(',', ': '))

def update_documentation(rules):
    """Generate documentation"""

    # Update the overview of all rules in the linter
    filename = 'docs/rules.md'

    # Sort rules by the Rule ID
    sorted_rules = sorted(rules, key=lambda obj: obj.id)

    data = []

    # Read current file up to the Rules part, everything up to that point is
    # static documentation.
    with open(filename, 'r') as original_file:

        line = original_file.readline()
        while line:
            data.append(line)

            if line == '## Rules\n':
                break

            line = original_file.readline()

    # Rebuild the file content
    with open(filename, 'w') as new_file:

        # Rewrite the static documentation
        for line in data:
            new_file.write(line)

        # Add the rules
        new_file.write(
            '(_This documentation is generated by running `cfn-lint --update-documentation`, do not alter this manually_)\n\n')
        new_file.write(
            'The following **{}** rules are applied by this linter:\n\n'.format(len(sorted_rules) + 3))
        new_file.write(
            '| Rule ID  | Title | Description | Config<br />(Name:Type:Default) | Source | Tags |\n')
        new_file.write('| -------- | ----- | ----------- | ---------- | ------ | ---- |\n')

        rule_output = '| {0}<a name="{0}"></a> | {1} | {2} | {3} | [Source]({4}) | {5} |\n'

        # Add system Errors (hardcoded)
        for error in [cfnlint.rules.ParseError(), cfnlint.rules.TransformError(), cfnlint.rules.RuleError()]:
            tags = ','.join('`{0}`'.format(tag) for tag in error.tags)
            new_file.write(rule_output.format(error.id, error.shortdesc, error.description, '', '', tags))

        # Separate the experimental rules
        experimental_rules = []

        for rule in sorted_rules:

            if rule.experimental:
                experimental_rules.append(rule)
                continue

            tags = ','.join('`{0}`'.format(tag) for tag in rule.tags)
            config = '<br />'.join('{0}:{1}:{2}'.format(key, values.get('type'), values.get('default'))
                                   for key, values in rule.config_definition.items())
            new_file.write(rule_output.format(rule.id, rule.shortdesc,
                                              rule.description, config, rule.source_url, tags))

        # Output the experimental rules (if any)
        if experimental_rules:
            new_file.write('### Experimental rules\n')
            new_file.write('| Rule ID  | Title | Description | Source | Tags |\n')
            new_file.write('| -------- | ----- | ----------- | ------ | ---- |\n')

            for rule in experimental_rules:
                tags = ','.join('`{0}`'.format(tag) for tag in rule.tags)
                config = '<br />'.join('{0}:{1}:{2}'.format(key, values.get('type'), values.get('default'))
                                       for key, values in rule.config_definition.items())
                new_file.write(rule_output.format(rule.id, rule.shortdesc,
                                                  rule.description, config, rule.source_url, tags))


def patch_spec(content, region):
    """Patch the spec file"""
    LOGGER.info('Patching spec file for region "%s"', region)

    append_dir = os.path.join(os.path.dirname(__file__), 'data', 'ExtendedSpecs', region)
    for dirpath, _, filenames in os.walk(append_dir):
        filenames.sort()
        for filename in fnmatch.filter(filenames, '*.json'):
            file_path = os.path.basename(filename)
            module = dirpath.replace('%s' % append_dir, '%s' % region).replace(os.path.sep, '.')
            LOGGER.info('Processing %s/%s', module, file_path)
            all_patches = jsonpatch.JsonPatch(cfnlint.helpers.load_resource(
                'cfnlint.data.ExtendedSpecs.{}'.format(module), file_path))

            # Process the generic patches 1 by 1 so we can "ignore" failed ones
            for all_patch in all_patches:
                try:
                    jsonpatch.JsonPatch([all_patch]).apply(content, in_place=True)
                except jsonpatch.JsonPatchConflict:
                    LOGGER.debug('Patch (%s) not applied in region %s', all_patch, region)
                except jsonpointer.JsonPointerException:
                    # Debug as the parent element isn't supported in the region
                    LOGGER.debug('Parent element not found for patch (%s) in region %s',
                                 all_patch, region)

    return content


def update_iam_policies():
    """update iam policies file"""

    url = 'https://awspolicygen.s3.amazonaws.com/js/policies.js'

    filename = os.path.join(
        os.path.dirname(cfnlint.data.AdditionalSpecs.__file__),
        'Policies.json')
    LOGGER.debug('Downloading policies %s into %s', url, filename)

    content = get_url_content(url)

    content = content.split('app.PolicyEditorConfig=')[1]
    content = json.loads(content)
    content['serviceMap']['Manage Amazon API Gateway']['Actions'].extend(
        ['HEAD', 'OPTIONS']
    )
    content['serviceMap']['Amazon Kinesis Video Streams']['Actions'].append(
        'StartStreamEncryption'
    )

    with open(filename, 'w') as f:
        json.dump(content, f, indent=2, sort_keys=True, separators=(',', ': '))
