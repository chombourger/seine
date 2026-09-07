# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The 'vendor:' spec section: resolving (resolve.py), fetching (fetch.py),
# the manifest/lock format (manifest.py), and 'seine vendor' itself
# (cli.py). Re-exported here so tests can patch e.g. HostBootstrap and
# every lazy 'from seine import vendor' call site sees it.

from seine.bootstrap import HostBootstrap
from seine import snapshot

from .manifest import (BUILD_CONTEXT, GRAPH_VERSION, MANIFEST, VendorPackage,
                       _binary_file_path, _binary_filename,
                       _binary_filename_legacy, _binary_hashes,
                       _cached_local_matches, _compress_binaries,
                       _compress_files, _expand_binaries, _expand_files,
                       _file_hashes, _local_sha1, _lock_sources,
                       _manifest_path, _reverse_of,
                       _save_source_snapshot_cache, _suite_distro,
                       architectures, deploy_repository, entries_for,
                       exclusions, extra_architectures, feeds_for_suite,
                       is_deployed, load_lock, load_manifest, lock_manifest,
                       manifest_digest, named_suites, offline_build_context,
                       offline_dockerfile_digest, parse, repository,
                       save_lock, save_manifest, suites, unconfigured_suites)
from .resolve import (REQUEST_FILE, RESOLVE_MOUNT, RESOLVE_SCRIPT,
                      RESPONSE_FILE, VENDOR_RESOLVER_IMAGE_SCRIPT,
                      VendorResolver, VendorResolverImage)
from .fetch import (_APT_SANDBOX_OPTS, _artifact_key, _binary_already_fetched,
                    _binary_has_gocode, _deb_has_gocode, _dedup_binaries,
                    _hardlink, _index_has_gocode, _link_fetched,
                    _lists_volume, _snapshot_fetch, fetch_binary,
                    fetch_source, index, keyring)
from .cli import (MAX_ATTEMPTS, USAGE, VendorCmd, _LiveFollower, _builder_for,
                  fetch_tasks, index_tasks, resolve_tasks)
