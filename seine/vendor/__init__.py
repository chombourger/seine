# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The 'vendor:' spec section: resolving it into a dependency graph
# (resolve.py), fetching what it names (fetch.py), the manifest/lock
# file format both of those read and write (manifest.py), and the
# task-graph glue plus 'seine vendor' itself (cli.py).
#
# Every name below is re-exported so 'from seine import vendor;
# vendor.<name>' (or 'from seine.vendor import <name>') keeps working
# exactly as it did with everything in one file. That includes
# 'HostBootstrap'/'snapshot'/'_builder_for'/'resolve_tasks'/
# 'fetch_tasks'/'index_tasks', which tests replace by patching this
# module's own attribute -- see cli.py's and resolve.py's own lazy
# 'from seine import vendor' at each call site, needed for the same
# reason a bare name there would never see that patch.

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
