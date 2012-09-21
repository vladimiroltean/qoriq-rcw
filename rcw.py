#!/usr/bin/python

# rcw.py -- compiles an RCW source file into an PBL/RCW binary

# Copyright 2011 Freescale Semiconductor, Inc.
# Author: Timur Tabi <timur@freescale.com>

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#     notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#     * Neither the name of Freescale Semiconductor nor the
#     names of its contributors may be used to endorse or promote products
#     derived from this software without specific prior written permission.

# This software is provided by Freescale Semiconductor "as is" and any
# express or implied warranties, including, but not limited to, the implied
# warranties of merchantability and fitness for a particular purpose are
# disclaimed. In no event shall Freescale Semiconductor be liable for any
# direct, indirect, incidental, special, exemplary, or consequential damages
# (including, but not limited to, procurement of substitute goods or services;
# loss of use, data, or profits; or business interruption) however caused and
# on any theory of liability, whether in contract, strict liability, or tort
# (including negligence or otherwise) arising in any way out of the use of
# this software, even if advised of the possibility of such damage.

# This program compiles an RCW "source file" into a binary image, with or
# without a PBL header.  The syntax of the source file is very simple.  These
# are the only constructs supported:
#
# 1. A bit field definition.  Bit fields are defined in the format XXX[b:e],
# where XXX is the name of the field (only letters, numbers, and an underscore
# are allowed), 'b' is the starting bit position, and 'e' is the ending bit
# position.
#
# 2. A value assigned to a bit field.  Assignments are defined in the format
# XXX=v, where XXX is the name of the field (which must have been previously
# defined), and 'v' is the numeric value assigned to that field.
#
# 3. A variable, which begins with a % symbol.  Any variable can by created, and
# it can be assigned any string.  However, specific variables have pre-defined
# meanings.  Variables are used to define parts of the RCW or PBL that cannot
# be defined by a symbol assignment.
#
# 4. A PBI can be defined in a .pbi/.end block.  Start the section with a line
# containing the string ".pbi".  The following PBI commands are available:
#
#   wait <n>        -- wait <n> cycles
#   write <a> <v>   -- write value <v> to address <a>
#   awrite <a> <v>  -- write value <v> to address <a>, with ACS bit set
#   flush           -- flush (perform a read at the addr of the previous write)
#
# Terminate the PBI section with ".end".
#
# The C pre-processor is invoked prior to parsing the source file.  This allows
# for C/C++ style comments and macros to be used in the source file.
#
# This program is provided as an informal tool for evaluation purposes only.
# It is not supported.  Please contact the author directly with any questions.

import os
import re
import sys
import struct
import binascii
import itertools
import subprocess

from optparse import OptionParser, OptionGroup

# An ordered dictionary.  This is like a normal dictionary, except that it's
# possible to iterate over the contents in the same order that they were added
# to the dictionary.  This allows us to display the entries in the .rcw file
# in the same order that they appear in the .rcwi file.
class ordered_dict(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self._order = self.keys()

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._order.remove(key)

    def order(self):
        return self._order[:]

    def ordered_items(self):
        return [(key,self[key]) for key in self._order]

# Python's binascii.crc32() function uses a different algorithm to calculate
# the CRC, so we need to do it manually.  The polynomial value is 0x04c11db7.
# Python integers can be larger than 32 bits, so we have to "& 0xffffffff"
# to keep the value within a 32-bit range.  The CRC algorithm depends on this.
# Otherwise, some calculations may overflow.
def crc32(data):
    # Calculate the CRC table
    table = []
    for i in range(256):
        mask = i << 24
        for j in range(8):
            if mask & 0x80000000:
                mask = (mask << 1) ^ 0x04c11db7;
            else:
                mask <<= 1;
        table.append(mask & 0xffffffff)

    crc = 0xffffffff
    for i in data:
        crc = (crc << 8) ^ table[(crc >> 24) ^ ord(i)]
        crc = crc & 0xffffffff

    return crc

# Command-line parser
def command_line():
    global options, args

    parser = OptionParser(usage='usage: %prog [options]',
        description='This script reads an RCW source file and generates an '
            'RCW bin file.')

    parser.add_option('-i', dest='input', help='input filename.  '
        'Defaults to stdin if not specified')

    parser.add_option('-o', dest='output', help='output filename.  '
        'Defaults to stdout if not specified')

    parser.add_option('--no-pbl', dest='pbl',
        help='do not generate the PBL preamble and end-command',
        action='store_false', default=True)

    parser.add_option('-r', dest='reverse', help='generate a source file from a binary.  '
        'Must also specify --rcw.  --pbl option is ignored.', action='store_true',
            default=False)

    parser.add_option('-I', dest='include', help='include path.  '
        'Can be specified multiple times', action="append")

    parser.add_option('--rcwi', dest='rcwi', help='RCWI defintion filename.  '
        'Used only if -r is specified.')

    (options, args) = parser.parse_args()

    if options.input:
        options.input = os.path.expanduser(options.input)
    else:
        options.input = '/dev/stdin'

    if options.output:
        options.output = os.path.expanduser(options.output)
    else:
        options.output = '/dev/stdout'

    if options.reverse and not options.rcwi:
        print "Error: -r option requires --rcw"
        sys.exit(1)

# Checks if the bits for the given field overlap those of another field that
# we've already parsed.
def check_for_overlap(name, begin, end):
    global symbols

    if name in symbols:
        print 'Error: Duplicate bitfield definition for', name
        return

    # Iterate over the list of symbols that have already been defined
    for n, [b, e] in symbols.iteritems():
        # check if either 'begin' or 'end' is inside an bitfield range
        if (b <= begin <= e) or (b <= end <= e):
            print 'Error: Bitfield', name, 'overlaps with', n

# Build a PBI section
def build_pbi(lines):
    subsection = ''

    for l in lines:
        if l[0] == 'wait':
            subsection += struct.pack('>LL', 0x091380c0, int(l[1], 0))
        elif l[0] == 'write':
            subsection += struct.pack('>LL', 0x09000000 + int(l[1], 0), int(l[2], 0))
        elif l[0] == 'awrite':
            subsection += struct.pack('>LL', 0x89000000 + int(l[1], 0), int(l[2], 0))
        elif l[0] == 'flush':
            subsection += struct.pack('>LL', 0x09138000, 0)
        else:
            print 'Unknown PBI subsection command "%s"' % l[0]
            return ''

    return subsection

# Parse a subsection
def parse_subsection(header, lines):
    if header == "pbi":
        return build_pbi(lines)

    print 'Error: unknown subsection "%s"' % header
    return ''

# Parse the .rcw file, one line at a time
def parse_source_file(source):
    global symbols
    global assignments
    global vars
    global pbi

    symbols = ordered_dict()

    in_subsection = False   # True == we're in a subsection
    pbi = ''

    for l2 in source:
        l2 = l2.split() # Convert into an array
        l = ''.join(l2) # Remove all whitespace

        if not len(l):  # Skip blank or comment-only lines
            continue

        # Is it a subsection?
        m = re.search(r'\.([a-zA-Z]+)', l)
        if m:
            if in_subsection:
                in_subsection = False
                pbi += parse_subsection(header, s)
            else:
                in_subsection = True
                header = m.group(1)
                s = []
            continue

        # Is it a subsection line?
        if in_subsection:
            s.append(l2)
            continue

        # Is it an identifier?  %var=value
        m = re.search(r'%([a-zA-Z]+)=(.+)', l)
        if m:
            identifier, value = m.groups()
            vars[identifier] = value
            continue

        # Is it a single field definition?  NAME[position]
        m = re.search(r'([A-Z0-9_]+)\[([0-9a-zA-Z]+)]', l)
        if m:
            name, position = m.groups()
            position = int(position, 0)
            check_for_overlap(name, position, position)

            symbols[name] = [position, position]
            continue

        # Is it a ranged field definition?  NAME[begin:end]
        m = re.search(r'([A-Z0-9_]+)\[([0-9a-zA-Z]+):([0-9a-zA-Z]+)\]', l)
        if m:
            (name, begin, end) = m.groups()
            begin = int(begin, 0)
            end = int(end, 0)
            check_for_overlap(name, begin, end)
            symbols[name] = [begin, end]
            continue

        # Is it a field assignment? NAME=value
        m = re.search(r'([A-Z0-9_]+)=([0-9a-zA-Z]+)', l)
        if m:
            (name, value) = m.groups()
            value = int(value, 0)
            if not name in symbols:
                print 'Error: Unknown bitfield', name
            else:
                if name in assignments:
                    print 'Error: Duplicate assignement for bitfield', name
                assignments[name] = value
            continue

        print 'Error: unknown command', ' '.join(l2)

# Return True if an executable program exists in the PATH somewhere
def find_program(filename):
    for path in os.environ["PATH"].split(os.pathsep):
        file = os.path.join(path, filename)
        if os.path.isfile(file) and os.access(file, os.X_OK):
            return True

    return False

# Run the C preprocessor on the given source code.  This allows you to include
# C macros and #include statements in the source file.
def read_source_file(filename):
    global options

    if not find_program('gcc'):
        print 'Could not find gcc in PATH'
        return None

    i = ['-I', '.']     # Always look in the current directory
    if options.include:
        for x in options.include:
            i.extend(['-I', x])
    p = subprocess.Popen(['gcc', '-E', '-x', 'c', '-P'] + i + [filename],
        shell=False, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ret = p.communicate()
    if p.returncode != 0:
        print ret[1],
        return None

    return ret[0].splitlines()

# Check for specific variables
def check_vars():
    global vars
    global options

    if not 'size' in vars:
        print 'Error: "%size" variable must be specified'
        sys.exit(1)

    if options.pbl:
        # If we want the PBL header/footer, the vars for those must be defined
        if not vars['sysaddr']:
            print 'Error: PBL format requires %sysaddr to be defined'
            sys.exit(1)

# Create a .bin file
def create_binary():
    global symbols
    global assignments
    global vars
    global options
    global pbi

    # Create the RCW data.  We encode it into 'bits' as a giant (2^size)-bit number
    size = int(vars['size'], 0)
    bits = 0L

    for n, v in assignments.iteritems():
        # n = name of symbol
        # v = value to assign
        b, e = symbols[n]   # First bit and last bit
        s = 1 + e - b       # number of bits in field

        # Make sure it's not too large
        if v >= (1 << s):
            print 'Error: Value', v, 'is too large for field', n
            continue

        # Set the bits.  We assume that bits [b:e] are already zero.  They can be
        # non-zero only if we have overlapping bitfield definitions, which we
        # already report as an error.
        bits += v << ((size - 1) - e)

    # Generate the binary.  First, apply the preamble, if requested
    binary = ''
    if options.pbl:
        length_byte = (((size / 8) & 63) << 1) | 1
        binary = binascii.unhexlify('aa55aa55') + chr(length_byte) + \
            binascii.unhexlify(vars['sysaddr'])

    # Then convert 'bits' into an array of bytes
    for i in range(size - 8, -1, -8):
        binary += chr(bits >> i & 0xff)

    # Add any PBI commands
    binary += pbi

    # Add the end-command
    if options.pbl:
        binary += binascii.unhexlify('08138040')

        # Calculate and add the CRC
        crc = crc32(binary) & 0xffffffff
        binary += struct.pack('>L', crc)

    return binary

# Create a source file from a binary and a .rcwi file
def create_source():
    global symbols
    global vars
    global options

    f = open(options.input, 'rb')
    binary = f.read()
    f.close()

    size = int(vars['size'], 0)

    # Insert the #include statement for the RCWI file.  We assume that the
    # file will be in the include path, so we use <> and strip any paths
    # from the filename.
    source = '#include <%s>\n\n' % os.path.basename(options.rcwi)

    # If the binary is larger than the RCW, then we assume that it has a
    # preamble and an end-command, so remove them.  This is bit hackish,
    # but it'll work for now.
    if len(binary) > (size / 8):
        preamble = binary[0:8]

        # Convert the binary into a large integer
        rcw = binary[8:8 + (size / 8)]
        bits = int(binascii.hexlify(rcw), 16)
    else:
        bits = int(binascii.hexlify(binary), 16)

    # Loop over all the known symbols
    for n, [b, e] in symbols.ordered_items():
        s = 1 + e - b       # number of bits in field

        shift = (size - 1) - e  # number of bits to shift
        mask = ((1 << s) - 1)
        v = (bits >> shift) & mask
        if v:
            source += "%s=%u\n" % (n, v)

            # Clear out the bits we just parsed, so that we can see if
            # there are any left over.  If there are, then it means that
            # there are bits set in the .bin that we don't recognize
            bits &= ~(mask << shift)

    if bits:
        print 'Unknown bits in positions:',
        mask = 1 << (size - 1)
        n = 0
        while mask:
            if (bits & mask):
                print n,
            n += 1
            mask >>= 1
        print

    return source

if (sys.version_info < (2,6)) or (sys.version_info >= (3,0)):
    print 'Only Python versions 2.6 or 2.7 are supported.'
    sys.exit(1)

# Make all 'print' statements output to stderr instead of stdout
sys.stdout = sys.stderr

command_line()

symbols = {}
assignments = {}
vars = {}
pbi = ''

if options.reverse:
    source = read_source_file(options.rcwi)
    if not source:
        sys.exit(1)
    parse_source_file(source)

    f = open(options.output, 'w')
    f.write(create_source())
    f.close()
else:
    source = read_source_file(options.input)
    if not source:
        sys.exit(1)
    parse_source_file(source)
    check_vars()

    # Write it all to the output file
    f = open(options.output, 'wb')
    f.write(create_binary())
    f.close()
