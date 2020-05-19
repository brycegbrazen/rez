import os
import os.path
import errno
from hashlib import sha1
from uuid import uuid4
import shutil
import subprocess
import sys
import platform
import time
import logging
import random
import threading
from contextlib import contextmanager

from rez.config import config
from rez.exceptions import PackageCacheError
from rez.vendor.lockfile import LockFile, NotLocked
from rez.utils import json
from rez.utils.filesystem import safe_listdir, safe_makedirs, safe_remove
from rez.utils.logging_ import print_info, print_warning
from rez.packages import get_variant


class PackageCache(object):
    """Package cache.

    A package cache is responsible for storing copies of variant payloads into a
    location that would typically be on local disk. The intent is to avoid
    fetching a package's files over shared storage at runtime.

    A package cache is used like so:

    * A rez-env is performed;
    * The context is resolved;
    * For each variant in the context, we check to see if it's present in the
      current package cache;
    * If it is, the variant's root is remapped to this location.

    A package cache is _not_ a package repository. It just stores copies of
    variant payloads - no package definitions are stored.

    Payloads are stored into the following structure:

        /<cache_dir>/foo/1.0.0/af8d/a/<payload>
                                   /a.json

    Here, 'af8d' is the first 4 chars of the SHA1 hash of the variant's 'handle',
    which is a dict of fields that uniquely identify the variant. To avoid
    hash collisions, the variant is then stored under a subdir that is incrementally
    named ('a', 'b', ..., 'aa', 'ab', ...). The 'a.json' file is used to find the
    correct variant within the hash subdir. The intent is to keep cached paths
    short, and avoid having to search too many variant.json files to find the
    matching variant.
    """

    VARIANT_NOT_FOUND = 0  # Variant was not found
    VARIANT_FOUND = 1  # Variant was found
    VARIANT_CREATED = 2  # Variant was created
    VARIANT_COPYING = 3  # Variant payload is still being copied to this cache
    VARIANT_COPY_STALLED = 4  # Variant payload copy has stalled
    VARIANT_PENDING = 5  # Variant is pending caching
    VARIANT_REMOVED = 6  # Variant was deleted

    _COPYING_TIME_INC = 0.2
    _COPYING_TIME_MAX = 5.0

    def __init__(self, path):
        """Create a package cache.

        Args:
            path (str): Path on disk, must exist.
        """
        if not os.path.isdir(path):
            raise PackageCacheError("Not a directory: %s" % path)

        self.path = path

        # make dirs for internal use
        safe_makedirs(self._log_dir)
        safe_makedirs(self._pending_dir)
        safe_makedirs(self._remove_dir)

    def get_cached_root(self, variant):
        """Get location of variant payload copy.

        Args:
            variant (`Variant`): Variant to search for.

        Returns:
            str: Cached variant root path, or None if not found.
        """
        status, rootpath = self._get_cached_root(variant)
        if status != self.VARIANT_FOUND:
            return None

        # touch the root path so we know when it was last used
        try:
            os.utime(rootpath, None)
        except OSError as e:
            if e.errno == errno.ENOENT:
                # maybe got cleaned up by other process
                return None
            else:
                raise

        return rootpath

    def add_variant(self, variant, force=False):
        """Copy a variant's payload into the cache.

        The following steps are taken to ensure muti-thread/proc safety, and to
        guarantee that a partially-copied variant payload is never able to be
        used:

        1. The hash dir (eg '/<cache_dir>/foo/1.0.0/af8d') is created;
        2. A file lock mutex ('/<cache_dir>/.lock') is acquired;
        3. The file '/<cache_dir>/foo/1.0.0/af8d/.copying-a' (or -b, -c etc) is
           created. This tells rez that this variant is being copied and cannot
           be used yet;
        4. The file '/<cache_dir>/foo/1.0.0/af8d/a.json' is created. Now
           another proc/thread can't create the same local variant;
        5. The file lock is released;
        6. The variant payload is copied to '/<cache_dir>/foo/1.0.0/af8d/a';
        7. The '.copying-a' file is removed.

        Args:
            variant (`Variant`): The variant to copy into this cache
            force (bool): Copy the variant regardless of its cachable attribute.
                Use at your own risk (there is no guarantee the resulting variant
                payload will be functional).

        Returns:
            2-tuple:
            - str: Path to cached payload
            - int: One of:
              - VARIANT_FOUND
              - VARIANT_CREATED
              - VARIANT_COPYING
              - VARIANT_COPY_STALLED
        """
        from rez.utils.base26 import get_next_base26
        from rez.utils.filesystem import safe_makedirs

        # do some sanity checking on variant to cache
        if not force and not variant.parent.is_cachable:
            raise PackageCacheError(
                "Package is not cachable: %s" % variant.parent.uri
            )

        variant_root = getattr(variant, "root", None)

        if not variant_root:
            raise PackageCacheError(
                "Cannot cache variant %s - it is a type of variant that "
                "does not have a root." % variant.uri
            )

        if not os.path.isdir(variant_root):
            raise PackageCacheError(
                "Cannot cache variant %s - its root does not appear to "
                "be present on disk (%s)." % variant.uri, variant_root
            )

        no_op_statuses = (
            self.VARIANT_FOUND,
            self.VARIANT_COPYING,
            self.VARIANT_COPY_STALLED
        )

        # variant already exists, or is being copied to cache by another thread/proc
        status, rootpath = self._get_cached_root(variant)
        if status in no_op_statuses:
            return (rootpath, status)

        # 1.
        path = self._get_hash_path(variant)
        safe_makedirs(path)

        # construct data to store to json file
        data = {
            "handle": variant.handle.to_dict()
        }

        if variant.index is not None:
            # just added for debugging purposes
            data["data"] = variant.parent.data["variants"][variant.index]

        # 2. + 5.
        with self._lock():
            # Check if variant exists again, another proc could have created it
            # just before lock acquire
            #
            status, rootpath = self._get_cached_root(variant)
            if status in no_op_statuses:
                return (rootpath, status)

            # determine next increment name ('a', 'b' etc)
            names = os.listdir(path)
            names = [x for x in names if x.endswith(".json")]

            if names:
                prev = os.path.splitext(max(names))[0]
            else:
                prev = None

            incname = get_next_base26(prev)

            # 3.
            copying_filepath = os.path.join(path, ".copying-" + incname)
            with open(copying_filepath, 'w'):
                pass

            # 4.
            json_filepath = os.path.join(path, incname + ".json")
            with open(json_filepath, 'w') as f:
                f.write(json.dumps(data))

        # 6.
        #
        # Here we continually update mtime on the .copying file, to indicate
        # that the copy is active. This allows us to detect stalled/errored
        # copies, and report them as VARIANT_COPY_STALLED status.
        #
        still_copying = True
        def _while_copying():
            while still_copying:
                time.sleep(self._COPYING_TIME_INC)
                os.utime(copying_filepath, None)

        rootpath = os.path.join(path, incname)
        th = threading.Thread(target=_while_copying)
        th.daemon = True
        th.start()

        try:
            shutil.copytree(variant_root, rootpath)
        finally:
            still_copying = False

        # 7.
        th.join()
        os.remove(copying_filepath)

        return (rootpath, self.VARIANT_CREATED)

    def remove_variant(self, variant):
        """Remove a variant from the cache.

        Since this removes the associated cached variant payload, there is no
        guarantee that this will not break packages currently in use by a
        context.

        Note that this does not actually free up associated disk space - you
        must call clean() to do that.

        Returns:
            int: One of:
            - VARIANT_REMOVED
            - VARIANT_NOT_FOUND
            - VARIANT_COPYING
        """
        status, rootpath = self._get_cached_root(variant)
        if status in (self.VARIANT_NOT_FOUND, self.VARIANT_COPYING):
            return status

        # If we got here, it's either a cached variant, or is stalled. In either
        # case, we get the lock, and remove all associated files. The payload
        # itself is moved into the system delete dir, ready for actual deletion
        # when clean() is called.
        #
        with self._lock():
            # move the payload
            dest_filename = variant.qualified_name + '-' + uuid4().hex
            dest_rootpath = os.path.join(self._remove_dir, dest_filename)

            try:
                os.rename(rootpath, dest_rootpath)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    # another proc may have just removed it
                    return self.VARIANT_REMOVED
                raise

            # delete json file
            path, incname = os.path.split(rootpath)
            filepath = os.path.join(path, incname + ".json")
            if os.path.exists(filepath):
                os.remove(filepath)

            # delete .copying file
            filepath = os.path.join(path, ".copying-" + incname)
            if os.path.exists(filepath):
                os.remove(filepath)

            # delete any dirs that are now empty
            for _ in range(3):  # hash-dir, version-dir, pkg-dir
                try:
                    os.rmdir(path)
                except OSError:
                    break  # not empty
                path = os.path.dirname(path)

        return self.VARIANT_REMOVED

    def add_variants_async(self, variants):
        """Update the package cache by adding some or all of the given variants.

        This method is called when a context is created or sourced. Variants
        are then added to the cache in a separate process.
        """
        variants_ = []

        # trim down to those variants that are cachable, and not already cached
        for variant in variants:
            if not variant.parent.is_cachable:
                continue

            status, _ = self._get_cached_root(variant)
            if status == self.VARIANT_NOT_FOUND:
                variants_.append(variant)

        if not variants_:
            return

        print_info("Caching %d variants in daeminized proc...", len(variants_))

        # Write each variant out to a file in the 'pending' dir in the cache. A
        # separate proc reads these files and then performs the actual variant
        # copy. Note that these files are unique, in case two rez procs attempt
        # to write out the same pending variant file at the same time.
        #
        pending_filenames = os.listdir(self._pending_dir)

        for variant in variants_:
            prefix = variant.parent.qualified_name + '-'
            handle_dict = variant.handle.to_dict()
            already_pending = False

            # check if this variant is already pending
            for filename in pending_filenames:
                if filename.startswith(prefix):
                    filepath = os.path.join(self._pending_dir, filename)
                    try:
                        with open(filepath) as f:
                            data = json.loads(f.read())
                    except:
                        continue  # maybe file was just deleted

                    if data == handle_dict:
                        already_pending = True
                        break

            if already_pending:
                continue

            filename = prefix + uuid4().hex + ".json"
            filepath = os.path.join(self._pending_dir, filename)
            with open(filepath, 'w') as f:
                f.write(json.dumps(handle_dict))

        # start caching subproc
        if platform.system() == "Windows":
            kwargs = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            }
        else:
            kwargs = {
                "preexec_fn": os.setsid
            }

        try:
            with open(os.devnull, 'w') as devnull:
                _ = subprocess.Popen(
                    ["rez-pkg-cache", "--daemon", self.path],
                    stdout=devnull,
                    stderr=devnull,
                    **kwargs
                )
        except Exception as e:
            print_warning("Failed to start package caching daemon: %s", e)

    def get_variants(self):
        """Get variants and their current statuses from the cache.

        Returns:
            List of 3-tuple:
            - `Variant`: The cached variant
            - str: Local cache path for variant, if determined ('' otherwise)
            - int: Status. One of:
              - VARIANT_FOUND
              - VARIANT_COPYING
              - VARIANT_COPY_STALLED
              - VARIANT_PENDING
        """
        from rez.packages import get_variant

        statuses = (
            self.VARIANT_FOUND,
            self.VARIANT_COPYING,
            self.VARIANT_COPY_STALLED
        )

        results = []
        seen_variants = set()

        # find variants in cache
        for pkg_name in safe_listdir(self.path):
            if pkg_name.startswith('.'):
                continue  # dirs for internal cache use

            path1 = os.path.join(self.path, pkg_name)

            for ver_str in safe_listdir(path1):
                path2 = os.path.join(path1, ver_str)

                for hash_str in safe_listdir(path2):
                    path3 = os.path.join(path2, hash_str)

                    for name in safe_listdir(path3):
                        if name.endswith(".json"):
                            with open(os.path.join(path3, name)) as f:
                                data = json.loads(f.read())

                            handle = data["handle"]
                            variant = get_variant(handle)

                            status, rootpath = self._get_cached_root(variant)
                            if status in statuses:
                                results.append((variant, rootpath, status))
                                seen_variants.add(variant)

        # find pending variants
        pending_filenames = os.listdir(self._pending_dir)

        for name in pending_filenames:
            filepath = os.path.join(self._pending_dir, name)

            try:
                with open(filepath) as f:
                    variant_handle_dict = json.loads(f.read())
            except:
                continue  # maybe file was just deleted

            variant = get_variant(variant_handle_dict)
            if variant not in seen_variants:
                results.append((variant, '', self.VARIANT_PENDING))
                seen_variants.add(variant)

        return results

    def run_daemon(self):
        """Run as daemon and copy pending variants.

        Called via `rez-pkg-cache --daemon`.
        """

        # daemonize if possible
        if platform.system() == "Windows":
            # Nothing we can do; subproc was launched with
            # creationflags=CREATE_NEW_PROCESS_GROUP, hopefully that's enough
            pass
        else:
            # https://stackoverflow.com/questions/6011235/run-a-program-from-python-and-have-it-continue-to-run-after-the-script-is-kille
            #
            # Note that subproc has been created with preexec_fn=os.setsid, so
            # first fork has already occurred
            #
            pid = os.fork()
            if pid > 0:
                sys.exit(0)

        logger = self._init_daemon_logging()
        logger.info("Started daemon")

        try:
            while True:
                keep_running = self._run_daemon_step(logger)
                if not keep_running:
                    break
        except Exception:
            logger.exception("An error occurred")
            raise

    @contextmanager
    def _lock(self):
        lock_filepath = os.path.join(self._sys_dir, ".lock")
        lock = LockFile(lock_filepath)

        try:
            lock.acquire(timeout=10)
            yield
        finally:
            try:
                lock.release()
            except NotLocked:
                pass

    def _run_daemon_step(self, logger):
        # pick a random pending variant to copy
        pending_filenames = os.listdir(self._pending_dir)
        if not pending_filenames:
            return False

        i = random.randint(0, len(pending_filenames) - 1)
        filename = pending_filenames[i]
        filepath = os.path.join(self._pending_dir, filename)

        try:
            with open(filepath) as f:
                variant_handle_dict = json.loads(f.read())
        except IOError as e:
            if e.errno == errno.ENOENT:
                return True  # was probably deleted by another rez-pkg-cache proc
            raise

        variant = get_variant(variant_handle_dict)

        # copy the variant and log activity
        logger.info("Started caching of variant %s...", variant.uri)
        t = time.time()

        try:
            rootpath, status = self.add_variant(variant)

        except PackageCacheError as e:
            # variant cannot be cached, so remove as a pending variant
            logger.warning(str(e))
            safe_remove(filepath)
            return True

        except Exception:
            # This is probably an error during shutil.copytree (eg a perms fail).
            # In this case, the variant will be in VARIANT_COPYING status, and
            # will shortly transition to VARIANT_COPY_STALLED. Thus we can
            # remove the pending variant, as there's nothing more we can do.
            #
            logger.exception("Failed to add variant to the cache")
            safe_remove(filepath)
            return True

        secs = time.time() - t

        if status == self.VARIANT_FOUND:
            logger.info("Variant was already cached at %s", rootpath)
        elif status == self.VARIANT_COPYING:
            logger.info("Variant is already being copied to %s", rootpath)
        elif status == self.VARIANT_COPY_STALLED:
            logger.info("Variant is stalled copying to %s", rootpath)
        else:  # VARIANT_CREATED
            logger.info("Cached variant to %s in %g seconds", rootpath, secs)

        if status != self.VARIANT_COPYING:
            safe_remove(filepath)

        return True

    def _init_daemon_logging(self):
        # create logger
        logger = logging.getLogger("rez-pkg-cache")
        logfilepath = os.path.join(self._log_dir, time.strftime("%Y-%m-%d.log"))
        handler = logging.FileHandler(logfilepath)
        formatter = logging.Formatter("%(name)s %(asctime)s pid(%(process)d) %(levelname)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # delete old logfiles
        try:
            now = int(time.time())

            for name in os.listdir(self._log_dir):
                filepath = os.path.join(self._log_dir, name)
                st = os.stat(filepath)
                age_secs = now - int(st.st_ctime)
                age_days = age_secs / (3600 * 24)
                if age_days > config.package_cache_log_days:
                    safe_remove(filepath)
        except:
            logger.exception("Failed to cleanup old logfiles")

        return logger

    @property
    def _sys_dir(self):
        return os.path.join(self.path, ".sys")

    @property
    def _log_dir(self):
        return os.path.join(self.path, ".sys", "log")

    @property
    def _pending_dir(self):
        return os.path.join(self.path, ".sys", "pending")

    @property
    def _remove_dir(self):
        return os.path.join(self.path, ".sys", "to_delete")

    def _get_cached_root(self, variant):
        path = self._get_hash_path(variant)
        if not os.path.exists(path):
            return (self.VARIANT_NOT_FOUND, '')

        handle_dict = variant.handle.to_dict()

        for name in os.listdir(path):
            if name.endswith(".json"):
                incname = os.path.splitext(name)[0]
                json_filepath = os.path.join(path, name)
                rootpath = os.path.join(path, incname)
                copying_filepath = os.path.join(path, ".copying-" + incname)

                try:
                    with open(json_filepath) as f:
                        data = json.loads(f.read())
                except IOError as e:
                    if e.errno == errno.ENOENT:
                        # maybe got cleaned up by other process
                        continue
                    else:
                        raise

                if data.get("handle") == handle_dict:
                    if os.path.exists(copying_filepath):
                        try:
                            st = os.stat(copying_filepath)
                            secs = time.time() - st.st_mtime
                            if secs > self._COPYING_TIME_MAX:
                                return (self.VARIANT_COPY_STALLED, rootpath)
                        except:
                            # maybe .copying file was deleted just now
                            pass

                        return (self.VARIANT_COPYING, rootpath)
                    else:
                        return (self.VARIANT_FOUND, rootpath)

        return (self.VARIANT_NOT_FOUND, '')

    def _get_hash_path(self, variant):
        dirs = [self.path, variant.name]

        if variant.version:
            dirs.append(str(variant.version))
        else:
            dirs.append("_NO_VERSION")

        h = sha1(str(variant.handle._hashable_repr()).encode('utf-8'))
        hash_dirname = h.hexdigest()[:4]
        dirs.append(hash_dirname)

        return os.path.join(*dirs)
