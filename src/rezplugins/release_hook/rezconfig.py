# SPDX-License-Identifier: Apache-2.0
# Copyright Contributors to the Rez Project


emailer = {
    # SMTP host.
    "smtp_host": '',

    # SMTP port.
    "smtp_port": 25,

    # The address that post-release emails appear to come from.
    "sender": '{system.user}@rez-release.com',

    # List of recipients of post-release emails; OR, path to recipients config
    # file (see emailer-recipients-example.yaml). If this is a string that
    # contains '@' and doesn't refer to a filepath, then it's treated as an
    # email address.
    "recipients": [],

    # Subject format - supports the same object formatting available in 'body'
    "subject": '[rez] [release] {system.user} released {package.qualified_name}',

    # Message format. Available objects for formatting are:
    # - package: The package that is being released.
    # - system: The system object.
    # - release: Namespace for info about the current release, contains:
    #   - path: Installation path of release.
    #   - message: Release message.
    #   - changelog: Release changelog.
    #   - previous_version: Version of previous release.
    # - variants: Namespace for info about the variants that were released:
    #   - count: The number of variants that were released;
    #   - paths: Newline-separated paths to the root of each variant.
    "body": """
Package '{package.qualified_name}' was released by {system.user}@{system.fqdn}.

USER: {system.user}
PACKAGE: {package.qualified_name}
RELEASED TO: {release.path}
PREVIOUS VERSION: {release.previous_version}
REZ VERSION: {system.rez_version}

{variants.count} VARIANTS:
{variants.paths}

MESSAGE:
{release.message}

CHANGELOG:
{release.changelog}
""".strip()
}

command = {
    # If true, print the commands that are being run
    "print_commands": True,

    # If true, print output of commands.
    "print_output": True,

    # If true, print failed commands to stderr
    "print_error": True,

    # If true, cancel the package release if a pre-* command fails.
    "cancel_on_error": True,

    # If true, skip all commands after a failed command. This does not cancel
    # the package release.
    "stop_on_error": True,

    # List of commands to execute prior to build, in given order.
    # Each item is a dict containing:
    # - 'command' (str): Command to execute, OR;
    # - 'args' (str or list of str) (optional): Arguments to the command. If a
    #   string, args are split by whitespace;
    # - 'pretty_args' (bool) (optional): Pretty format args; if True, then, ie,
    #   top-level lists are printed as "1 2 3", instead of "[1, 2, 3]"; defaults
    #   to True
    # - 'user' (str) (optional): User to execute the commands as, defaults to
    #   current user.
    # - 'env' (dict): Environment variables to set for the command. They are
    #   added to the current env.
    #
    # Command arguments are formatted and the following objects are available:
    # - package: The package that is being released.
    # - system: The system object.
    # - release: Namespace for info about the current release, contains:
    #   - path: Installation path of release.
    #
    # Any environment variables references in command arguments are expanded.
    #
    # Example:
    # pre_build_commands
    # - command: ls
    #   args: '-a -l'
    #   user: root
    #
    "pre_build_commands": [],

    # Same expected values as pre_build_commands
    "pre_release_commands": [],

    # Same expected values as pre_build_commands
    "post_release_commands": []
}

amqp = {
    # host server, or '{host}:{port}'
    "host": '',

    # userid
    "userid": '',

    # password
    "password": '',

    # connection timeout
    "connect_timeout": 10,

    # exchange name
    "exchange_name": '',

    # exchange routing key
    "exchange_routing_key": 'REZ.PACKAGE.RELEASED',

    # message delivery mode
    "message_delivery_mode": 1,

    # extra message attributes to be published
    "message_attributes": {}
}
