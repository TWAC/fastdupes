#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Find Dupes Fast
By Stephan Sokolow (ssokolow.com)

A simple script which identifies duplicate files several orders of magnitude
more quickly than fdupes by using smarter algorithms.

--snip--

@todo:
 - Once ready, announce this in a comment at
   U{http://ubuntu.wordpress.com/2005/10/08/find-duplicate-copies-of-files/}
 - Look into possible solutions for pathological cases of thousands of files
   with the same size and same pre-filter results. (File handle exhaustion)
 - Run this through a memory profiler and look for obvious bloat to trim.
 - Look into supporting gettext localization.

@newfield appname:Application Name

Copyright (C) 2009-2014 Stephan Sokolow

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, see <http://www.gnu.org/licenses/>.
"""


__appname__ = "Find Dupes Fast"
__author__ = "Stephan Sokolow (deitarion/SSokolow)"
__version__ = "0.3.6"
__license__ = "GNU GPL 2.0 or later"

import fnmatch, os, re, stat, sys
from functools import wraps

# Note: In my `python -m timeit` tests, the difference between MD5 and SHA1 was
# negligible, so there is no meaningful reason not to take advantage of the
# reduced potential for hash collisions that SHA1's greater hash size offers.
import hashlib

#: Default settings used by C{optparse} and some functions
DEFAULTS = {
    'delete': False,
    'exclude': ['*/.svn', '*/.bzr', '*/.git', '*/.hg'],
    'min_size': 25,  # Only check files this big or bigger.
}
CHUNK_SIZE = 2 ** 16  #: Size for chunked reads from file handles
HEAD_SIZE = 2 ** 14  #: Limit how many bytes will be read to compare headers

#{ General Helper Functions

# We need os.lstat so we can skip symlinks, but we want Windows portability too
try:
    _stat = os.lstat
except AttributeError:
    _stat = os.stat

def multiglob_compile(globs, prefix=False):
    """Generate a single "A or B or C" regex from a list of shell globs.

    @param globs: An iterable of strings to be processed by C{fnmatch}.
    @param prefix: If C{True}, then C{match()} will perform prefix matching
                   rather than exact string matching.
    @type globs: iterable of C{str}
    @type prefix: C{bool}

    @todo: Also use this for excludes.
    """
    if not globs:
        # An empty globs list should only match empty strings
        return re.compile('^$')
    elif prefix:
        globs = [x + '*' for x in globs]
    return re.compile('|'.join(fnmatch.translate(x) for x in globs))

def hashFile(handle, want_hex=False, limit=None, chunk_size=CHUNK_SIZE):
    """Generate an SHA1 hash for a potentially long file.
    Digesting will obey L{CHUNK_SIZE} to conserve memory.

    @param handle: A file-like object or path to hash from.
    @param want_hex: If true, the returned hash will be hex-encoded.
    @param limit: The maximum number of bytes to read (will be rounded up to
        a multiple of C{CHUNK_SIZE})
    @param chunk_size: Size of C{read()} operations in bytes.

    @type want_hex: C{bool}
    @type limit: C{int}
    @type chunk_size: C{int}

    @rtype: C{str}
    @returns: A binary or hex-encoded SHA1 hash.

    @note: It is your responsibility to close any file-like objects you pass in
    """
    fhash, read = hashlib.sha1(), 0
    if isinstance(handle, basestring):
        handle = file(handle, 'rb')

    if limit:
        chunk_size = min(chunk_size, limit)

    # Chunked digest generation (conserve memory)
    for block in iter(lambda: handle.read(chunk_size), ''):
        fhash.update(block)
        read += chunk_size
        if 0 < limit <= read:
            break

    return want_hex and fhash.hexdigest() or fhash.digest()

class OverWriter(object):  # pylint: disable=too-few-public-methods
    """Output helper for handling overdrawing the previous line cleanly."""
    def __init__(self, fobj):
        self.max_len = 0
        self.fobj = fobj

        self.isatty = False
        if hasattr(self.fobj, 'fileno'):
            self.isatty = os.isatty(self.fobj.fileno())

    def write(self, text, newline=False):
        """Use CR to overdraw the current line with the given text.

        This function transparently handles tracking how much overdrawing is
        necessary to erase the previous line when used consistently.

        @param text: The text to be outputted
        @param newline: Whether to start a new line and reset the length count.
        @type text: C{str}
        @type newline: C{bool}
        """
        if not self.isatty:
            self.fobj.write('%s\n' % text)
            return

        msg_len = len(text)
        self.max_len = max(self.max_len, msg_len)

        self.fobj.write("\r%-*s" % (self.max_len, text))
        if newline or not self.isatty:
            self.fobj.write('\n')
            self.max_len = 0

out = OverWriter(sys.stderr)

def path_is_descendant(ancestor, descendant):
    """Check whether a given path is a descendant of another given path.

    @note: You can't just use C{startswith()} without adding C{os.sep} because,
           without it, you'll match siblings with a common prefix and
           normalization strips trailing separators.
    """
    ancestor = os.path.normcase(os.path.normpath(ancestor))
    descendant = os.path.normcase(os.path.normpath(descendant))
    return (descendant + os.sep).startswith(ancestor + os.sep)

#}
#{ Processing Pipeline

def getPaths(roots, ignores=None):
    """
    Convert a list of paths containing directories into a list of absolute file
    paths.

    @param roots: Files and folders to walk.
    @param ignores: A list of shell globs to avoid walking and omit from
                    results.

    @returns: List of paths containing only files.
    @rtype: C{list}

    @todo: Try to optimize the ignores matching. Running a regex on every
    filename is a fairly significant percentage of the time taken according to
    the profiler.
    """
    paths, count, ignores = [], 0, ignores or []

    # Prepare the ignores list for most efficient use
    ignore_re = multiglob_compile(ignores, prefix=False)

    for root in roots:
        # For safety, only use absolute, real paths.
        root = os.path.realpath(root)

        # Handle directly-referenced filenames properly
        # (And override ignores to "do as I mean, not as I say")
        if os.path.isfile(root):
            paths.append(root)
            continue

        for fldr in os.walk(root):
            out.write("Gathering file paths to compare... (%d files examined)"
                      % count)

            # Don't even descend into IGNOREd directories.
            for subdir in fldr[1]:
                dirpath = os.path.join(fldr[0], subdir)
                if ignore_re.match(dirpath):
                    fldr[1].remove(subdir)

            for filename in fldr[2]:
                filepath = os.path.join(fldr[0], filename)
                if ignore_re.match(filepath):
                    continue  # Skip IGNOREd files.

                paths.append(filepath)
                count += 1

    out.write("Found %s files to be compared for duplication." % (len(paths)),
              newline=True)
    return paths

def groupBy(groups_in, classifier, fun_desc='?', keep_uniques=False,
            *args, **kwargs):
    """Subdivide groups of paths according to a function.

    @param groups_in: Groups of path lists.
    @param classifier: Function which takes an iterable of paths, C{*args} and
        C{**kwargs} and subdivides the iterable, returning a dict mapping keys
        to new groups.
    @param fun_desc: Human-readable term for what paths are being grouped
        by for use in log messages.
    @param keep_uniques: If false, discard groups with only one member.

    @type groups_in: C{dict} of iterables
    @type classifier: C{function(str, dict)}
    @type fun_desc: C{str}
    @type keep_uniques: C{bool}

    @returns: A dict mapping sizes to lists of paths.
    @rtype: C{dict}

    @attention: Grouping functions generally use a C{set} for C{groups} as
        extra protection against accidentally counting a given file twice.
        (Complimentary to C{os.path.realpath()} in L{getPaths})
    """
    groups, count, group_count = {}, 0, len(groups_in)
    for pos, paths in enumerate(groups_in.values()):
        out.write("Subdividing group %d of %d by %s... (%d files examined, %d "
                  "in current group)" % (
                      pos + 1, group_count, fun_desc, count, len(paths)
                  ))

        # TODO: Find some way to bring back the file-by-file status text
        for key, group in classifier(paths, *args, **kwargs).items():
            groups.setdefault(key, set()).update(group)
            count += len(group)

    if not keep_uniques:
        # Return only the groups with more than one file.
        groups = dict([(x, groups[x]) for x in groups if len(groups[x]) > 1])

    out.write("Found %s sets of files with identical %s. (%d files examined)"
              % (len(groups), fun_desc, count), newline=True)
    return groups

def groupify(function):
    """Decorator to convert a function which takes a single value and returns
    a key into one which takes a list of values and returns a dict of key-group
    mappings.

    @returns: A dict mapping keys to groups of values.
    @rtype: C{{object: set(), ...}}
    """

    @wraps(function)
    def wrapper(paths, *args, **kwargs):
        groups = {}

        for path in paths:
            key = function(path, *args, **kwargs)
            if key is not None:
                groups.setdefault(key, set()).add(path)

        return groups
    return wrapper

@groupify
def sizeClassifier(path, min_size=DEFAULTS['min_size']):
    """Sort a file into a group based on on-disk size.

    @param path: The path to the file.
    @param min_size: Files smaller than this size (in bytes) will be ignored.

    @type path: C{str}
    @type min_size: C{int}

    @returns: The file size for use as a hash bucket ID.
    @rtype: C{int}

    @todo: Rework the calling of stat() to minimize the number of calls. It's a
    fairly significant percentage of the time taken according to the profiler.
    """
    filestat = _stat(path)
    if stat.S_ISLNK(filestat.st_mode):
        return  # Skip symlinks.

    if filestat.st_size < min_size:
        return  # Skip files below the size limit

    return filestat.st_size

@groupify
def hashClassifier(path, limit=HEAD_SIZE):
    """Sort a file into a group based on its SHA1 hash.

    @param path: The path to the file.
    @param limit: Only this many bytes will be counted in the hash.
        Values which evaluate boolean False indicate no limit.

    @type path: C{str}
    @type limit: C{int}

    @returns: The file's hash for use as a hash bucket ID.
    @rtype: C{str}

    """
    return hashFile(path, limit=limit)

def groupByContent(paths):
    """Byte-for-byte comparison on an arbitrary number of files in parallel.

    This operates by opening all files in parallel and comparing
    chunk-by-chunk. This has the following implications:
        - Reads the same total amount of data as hash comparison.
        - Performs a I{lot} of disk seeks. (Best suited for SSDs)
        - Vulnerable to file handle exhaustion if used on its own.

    @param paths: List of potentially identical files.
    @type paths: iterable

    @returns: A dict mapping one path to a list of all paths (self included)
              with the same contents.
    @rtype: C{dict}

    @todo: Start examining the C{while handles:} block to figure out how to
        minimize thrashing in situations where read-ahead caching is active.
        Compare savings by read-ahead to savings due to eliminating false
        positives as quickly as possible. This is a 2-variable min/max problem.
    """
    handles, results = [], []

    # Silently ignore files we don't have permission to read.
    hList = []
    for path in paths:
        try:
            hList.append((path, open(path, 'rb'), ''))
        except IOError:
            pass  # TODO: Verbose-mode output here.
    handles.append(hList)

    while handles:
        # Process more blocks.
        more, done = compareChunks(handles.pop(0))

        # Add the results to the top-level lists.
        handles.extend(more)
        results.extend(done)

    # Keep the same API as the others.
    return dict((x[0], x) for x in results)

def compareChunks(handles, chunkSize=CHUNK_SIZE):
    """Group a list of file handles based on equality of the next chunk of
    data read from them.

    @param handles: A list of open handles for file-like objects with
        potentially-identical contents.
    @param chunkSize: The amount of data to read from each handle every time
        this function is called.

    @returns: Two lists of lists:
     - One containing more lists to be fed back into this function individually
     - One containing finished groups of duplicate paths. (includes unique
       files as single-file lists)
    @rtype: C{(list, list)}

    @attention: File handles will be automatically-closed when no longer needed
    @todo: Discard the chunk contents immediately once they're no longer needed
    """
    chunks = [(path, fh, fh.read(chunkSize)) for path, fh, _ in handles]
    more, done = [], []

    # While there are combinations not yet tried...
    while chunks:
        # Compare the first chunk to all successive chunks
        matches, non_matches = [chunks[0]], []
        for chunk in chunks[1:]:
            if matches[0][2] == chunk[2]:
                matches.append(chunk)
            else:
                non_matches.append(chunk)
        # Check for EOF or obviously unique files
        if len(matches) == 1 or matches[0][2] == "":
            for x in matches:
                x[1].close()
            done.append([x[0] for x in matches])
        else:
            more.append(matches)
        chunks = non_matches

    return more, done

#}
#{ User Interface

def pruneUI(dupeList, mainPos=1, mainLen=1):
    """Display a list of files and prompt for ones to be kept.

    The user may enter "all" or one or more numbers separated by spaces and/or
    commas.

    @note: It is impossible to accidentally choose to keep none of the
        displayed files.

    @param dupeList: A list duplicate file paths
    @param mainPos: Used to display "set X of Y"
    @param mainLen: Used to display "set X of Y"
    @type dupeList: C{list}
    @type mainPos: C{int}
    @type mainLen: C{int}

    @returns: A list of files to be deleted.
    @rtype: C{list}
    """
    dupeList = sorted(dupeList)
    print
    for pos, val in enumerate(dupeList):
        print "%d) %s" % (pos + 1, val)
    while True:
        choice = raw_input("[%s/%s] Keepers: " % (mainPos, mainLen)).strip()
        if not choice:
            print ("Please enter a space/comma-separated list of numbers or "
                   "'all'.")
            continue
        elif choice.lower() == 'all':
            return []
        try:
            out = [int(x) - 1 for x in choice.replace(',', ' ').split()]
            return [val for pos, val in enumerate(dupeList) if pos not in out]
        except ValueError:
            print("Invalid choice. Please enter a space/comma-separated list"
                  "of numbers or 'all'.")

#}

def find_dupes(paths, exact=False, ignores=None, min_size=0):
    """High-level code to walk a set of paths and find duplicate groups.

    @param exact: Whether to compare file contents by hash or by reading
                  chunks in parallel.

    See L{getPaths} and L{sizeClassifier} for more argument documentation.

    @returns: A list of lists representing du"""
    groups = {'': getPaths(paths, ignores)}
    groups = groupBy(groups, sizeClassifier, 'sizes', min_size=min_size)

    # This serves one of two purposes depending on run-mode:
    # - Minimize number of files checked by full-content comparison (hash)
    # - Minimize chances of file handle exhaustion and limit seeking (exact)
    groups = groupBy(groups, hashClassifier, 'header hashes', limit=HEAD_SIZE)

    if exact:
        groups = groupBy(groups, groupByContent, fun_desc='contents')
    else:
        groups = groupBy(groups, hashClassifier, fun_desc='hashes')

    return groups

def print_defaults():
    """Display the default values for all command-line options"""
    maxlen = max([len(x) for x in DEFAULTS])
    for key in DEFAULTS:
        value = DEFAULTS[key]
        if isinstance(value, (list, set)):
            value = ', '.join(value)
        print "%*s: %s" % (maxlen, key, value)

def delete_dupes(groups, prefer_list=None, interactive=True, dry_run=False):
    """Code to handle the --delete command-line option."""
    prefer_list = prefer_list or []
    prefer_re = multiglob_compile(prefer_list, prefix=True)

    for pos, group in enumerate(groups.values()):
        # TODO: Add a secondary check for symlinks for safety.
        preferred = [x for x in group if prefer_re.match(x)]
        pruneList = [x for x in group if x not in preferred]
        if not preferred:
            if interactive:
                pruneList = pruneUI(group, pos + 1, len(groups))
                preferred = [x for x in group if x not in pruneList]
            else:
                preferred, pruneList = pruneList, []

        assert preferred  # Safety check
        for path in pruneList:
            print "Removing %s" % path
            if not dry_run:
                os.remove(path)

def main():
    """The main entry point, compatible with setuptools entry points."""
    # pylint: disable=bad-continuation
    from optparse import OptionParser, OptionGroup
    parser = OptionParser(usage="%prog [options] <folder path> ...",
            version="%s v%s" % (__appname__, __version__))
    parser.add_option('-D', '--defaults', action="store_true", dest="defaults",
        default=False, help="Display the default values for options which take"
        " arguments and then exit.")
    parser.add_option('-E', '--exact', action="store_true", dest="exact",
        default=False, help="There is a vanishingly small chance of false"
        " positives when comparing files using sizes and hashes. This option"
        " enables exact comparison. However, exact comparison requires a lot"
        " of disk seeks, so, on traditional moving-platter media, this trades"
        " a LOT of performance for a very tiny amount of safety most people"
        " don't need.")
    # XXX: Should I add --verbose and/or --quiet?

    filter_group = OptionGroup(parser, "Input Filtering")
    filter_group.add_option('-e', '--exclude', action="append", dest="exclude",
        metavar="PAT", help="Specify a globbing pattern to be"
        " added to the internal blacklist. This option can be used multiple"
        " times. Provide a dash (-) as your first exclude to override the"
        " pre-programmed defaults.")
    filter_group.add_option('--min-size', action="store", type="int",
        dest="min_size", metavar="X", help="Specify a non-default minimum size"
        ". Files below this size (default: %default bytes) will be ignored.")
    parser.add_option_group(filter_group)

    behaviour_group = OptionGroup(parser, "Output Behaviour")
    behaviour_group.add_option('-d', '--delete', action="store_true",
        dest="delete", help="Prompt the user for files to preserve and delete "
                            "all others.")
    behaviour_group.add_option('-n', '--dry-run', action="store_true",
        dest="dry_run", metavar="PREFIX", help="Don't actually delete any "
        "files. Just list what actions would be performed. (Good for testing "
        "values for --prefer)")
    behaviour_group.add_option('--prefer', action="append", dest="prefer",
        metavar="PATH", default=[], help="Append a globbing pattern which "
        "--delete should automatically prefer (rather than prompting) when it "
        "occurs in a list of duplicates.")
    behaviour_group.add_option('--noninteractive', action="store_true",
        dest="noninteractive", help="When using --delete, automatically assume"
        " 'all' for any groups with no --prefer matches rather than prompting")
    parser.add_option_group(behaviour_group)
    parser.set_defaults(**DEFAULTS)  # pylint: disable=W0142

    opts, args = parser.parse_args()

    if '-' in opts.exclude:
        opts.exclude = opts.exclude[opts.exclude.index('-') + 1:]
    opts.exclude = [x.rstrip(os.sep + (os.altsep or '')) for x in opts.exclude]
    # This line is required to make it match directories

    if opts.defaults:
        print_defaults()
        sys.exit()

    groups = find_dupes(args, opts.exact, opts.exclude, opts.min_size)

    if opts.delete:
        delete_dupes(groups, opts.prefer, not opts.noninteractive,
                     opts.dry_run)
    else:
        for dupeSet in groups.values():
            print '\n'.join(dupeSet) + '\n'

if __name__ == '__main__':
    main()

# vim: set sw=4 sts=4 expandtab :
