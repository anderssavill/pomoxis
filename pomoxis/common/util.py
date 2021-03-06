import argparse
import itertools
import re
import shutil
import sys

from Bio import SeqIO
from collections import namedtuple
import numpy as np
from pysam import FastxFile


_region_decoder_ = re.compile(r"(?P<ref_name>\w+):*(?P<start>(\d+-)*)(?P<end>\d*)")
Region = namedtuple('Region', 'ref_name start end')


def chunks(iterable, n):
    """Generate fixed length chunks of an interable.

    :param iterable: input sequence.
    :param n: chunk size.
    """
    it = iter(iterable)
    while True:
        chunk_it = itertools.islice(it, n)
        try:
            first_el = next(chunk_it)
        except StopIteration:
            return
        yield itertools.chain((first_el,), chunk_it)


def cat(files, output, chunks=1024*1024*10):
    """Concatenate a set of files.

    :param files: input filenames.
    :param output: output filenames.
    :param chunks: buffersize for filecopy.
    """
    with open(output,'wb') as wfd:
        for f in files:
            with open(f, 'rb') as fd:
                shutil.copyfileobj(fd, wfd, chunks)


def split_fastx(fname, output, chunksize=10000):
    """Split records in a fasta/q into fixed lengths.

    :param fname: input filename.
    :param output: output filename.
    :param chunksize: (maximum) length of output records.
    """
    with open(output, 'w') as fout:
        with FastxFile(fname, persist=False) as fin:
            for rec in fin:
                name = rec.name
                seq = rec.sequence
                qual = rec.quality
                if rec.comment is None:
                    comment = 'chunk_length={}'.format(chunksize)
                else:
                    comment = '{} chunk_length={}'.format(rec.comment, chunksize)
                if qual is None:
                    for i, s in enumerate(chunks(seq, chunksize)):
                        chunk_name = '{}_chunk{}'.format(name, i)
                        fout.write(">{} {}\n{}\n".format(
                            chunk_name, comment, ''.join(s)))
                else:
                    for i, (s, q) in enumerate(zip(chunks(seq, chunksize), chunks(qual, chunksize))):
                        chunk_name = '{}_chunk{}'.format(name, i)
                        fout.write('@{} {}\n{}\n+\n{}\n'.format(
                            chunk_name, comment, ''.join(s), ''.join(q)))


def split_fastx_cmdline():
    """Split records in a fasta/q file into chunks of a maximum size."""
    fname, output, chunksize = sys.argv[1:]
    split_fastx(fname, output, int(chunksize))


def fast_convert():
    """Convert between fasta<->fastq."""

    parser = argparse.ArgumentParser(description='fast_convert -- Convert between fasta<->fastq.')
    parser.add_argument('convert', choices=['qq', 'aa', 'aq', 'qa'],
        help='Conversion code: from->to.')
    parser.add_argument('--discard_q', action='store_true',
        help='Discard quality information from fastq, use with --mock_q.')
    parser.add_argument('--mock_q', default=10, type=int,
        help='Mock quality value, valid for convert=aq|qq.')
    args = parser.parse_args()

    in_fmt = 'fastq'
    out_fmt= 'fasta'
    qflag = False # controls quality manipulation
    if args.convert == 'qq':
        out_fmt = 'fastq'
        if args.discard_q is not None:
            qflag = True
    elif args.convert == 'aa':
        in_fmt = 'fasta'
    elif args.convert == 'aq':
        in_fmt = 'fasta'
        out_fmt = 'fastq'
        qflag = True
    elif args.convert == 'qa':
        pass # default
    else:
        raise ValueError("convert must be 'qq', 'aq', 'qa', or 'aa'\n")

    if qflag:
        def fq_gen(io):
            for rec in SeqIO.parse(io, in_fmt):
                rec.letter_annotations["phred_quality"] = [args.mock_q] * len(rec)
                yield rec
        sys.stderr.write('Creating/ignoring quality information in input.\n')
        SeqIO.write(fq_gen(sys.stdin), sys.stdout, out_fmt)
    else:
        SeqIO.convert(sys.stdin, in_fmt, sys.stdout, out_fmt)


def extract_long_reads():
    """Filter fastq to longest reads."""

    parser = argparse.ArgumentParser(description='Extract longest reads from a fastq.')
    parser.add_argument('input',
        help='Input .fastq file.')
    parser.add_argument('output',
        help='Output .fastq file.')
    filt = parser.add_mutually_exclusive_group(required=True)
    filt.add_argument('--longest', default=None, type=int,
        help='Percentage of longest reads to partition.')
    filt.add_argument('--bases', default=None, type=int,
        help='Maximum number of bases (subject to at least one read.)')
    parser.add_argument('--others', default=None,
        help='Write all other reads to file.')
    args = parser.parse_args()

    sys.stderr.write('Loading reads...\n')
    fmt = 'fastq'
    try:
        record_dict = SeqIO.index(args.input, fmt)
    except:
        fmt = 'fasta'
        record_dict = SeqIO.index(args.input, fmt)
    sys.stderr.write('Format is {}.\n'.format(fmt))

    ids = list(record_dict.keys())
    lengths = np.fromiter(
        (len(record_dict[i]) for i in ids),
        dtype=int, count=len(ids)
    )
    sys.stderr.write('Sorting reads...\n')
    if args.bases is None:
        # partial sort will do fine here
        max_reads = int(len(ids) * (args.longest / 100))
        longest = np.argpartition(lengths, -max_reads)[-max_reads:]
    else:
        # need a full sort
        order = np.argsort(lengths)[::-1]
        cumsum = 0
        last = 1
        for i, j in enumerate(np.argsort(lengths), 1):
            cumsum += lengths[j]
            if cumsum > args.bases:
                break
            last = i
        longest = order[:last]

    SeqIO.write(
        (record_dict[ids[i]] for i in longest),
        args.output, fmt
    )

    if args.others is not None:
        longest = set(longest)
        SeqIO.write(
            (record_dict[ids[i]] for i in range(len(ids)) if i not in longest),
            args.others, fmt
        )


def parse_regions(regions, ref_lengths=None):
    """Parse region strings into `Region` objects.

    :param regions: iterable of str
    :param ref_lengths: {str ref_names: int ref_lengths}, if provided Region.end
        will default to the reference length instead of None.

    >>> parse_regions(['Ecoli'])[0]
    Region(ref_name='Ecoli', start=0, end=None)
    >>> parse_regions(['Ecoli:1000-2000'])[0]
    Region(ref_name='Ecoli', start=1000, end=2000)
    >>> parse_regions(['Ecoli:1000'])[0]
    Region(ref_name='Ecoli', start=0, end=1000)
    >>> parse_regions(['Ecoli:500-'])[0]
    Region(ref_name='Ecoli', start=500, end=None)
    >>> parse_regions(['Ecoli'], ref_lengths={'Ecoli':4800000})[0]
    Region(ref_name='Ecoli', start=0, end=4800000)

    """
    decoded = []
    for region in regions:
        d =  _region_decoder_.match(region).groupdict()
        d['start'] = d['start'].replace('-', '')
        for key in ['start', 'end']:
            d[key] = None if d[key] == '' else int(d[key])
        d['start'] = 0 if d['start'] is None else d['start']
        if d['end'] is None and ref_lengths is not None:
            d['end'] = ref_lengths[d['ref_name']]
        decoded.append(Region(**d))
    return tuple(decoded)
