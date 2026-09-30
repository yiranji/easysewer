"""Bounded RPT decoding and a lossless catalog; table semantics live separately."""

from dataclasses import dataclass
from datetime import datetime
import codecs
import copy
import hashlib
from pathlib import Path
import re
import stat
import weakref

from ..model.identity import Ref
from ..validation import Diagnostic, Severity, SourceSpan, ValidationReport
from ..validation._cooperative import checkpoint as work_checkpoint, checkpointed, checkpoint_scope


SWMM_UTF8_REPORT = 'epa-swmm:5.2.4:utf8-report'
DEFAULT_REPORT_LIMIT = 64 * 1024 * 1024

# Match splitlines(keepends=True) without retaining one object per physical
# line. Text and bytes deliberately have different splitlines boundaries.
_TEXT_LINE = re.compile(r'[^\r\n\v\f\x1c-\x1e\x85\u2028\u2029]*(?:\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]|$)')
_BYTE_LINE = re.compile(rb'[^\r\n]*(?:\r\n|[\r\n]|$)')
_VERIFIED_REPORTS = {}


def _remember_source(document):
    # The parser creates these exact immutable record types. Do not retain a
    # subclass or arbitrary caller-supplied metadata as trusted parse evidence.
    if type(document) is not ReportDocument:
        return
    if any(value is not None and type(value) is not str for value in
           (document.text, document.source, document.profile, document.requested_encoding)):
        return
    key = id(document)

    def discard(reference):
        current = _VERIFIED_REPORTS.get(key)
        if current is not None and current[0] is reference:
            del _VERIFIED_REPORTS[key]

    # Copy the record structure, not its immutable strings/bytes. This also
    # detects mutation of nested frozen records via object.__setattr__. The
    # snapshot has no reference back to document, so it dies with the weak key.
    work_checkpoint()
    snapshot = copy.deepcopy(document)
    work_checkpoint()
    _VERIFIED_REPORTS[key] = (weakref.ref(document, discard), snapshot)


def _matches_source(document):
    retained = _VERIFIED_REPORTS.get(id(document))
    if retained is not None and retained[0]() is document and document == retained[1]:
        return True
    observed = ReportDocument.from_bytes(document.raw, encoding=document.requested_encoding,
        source=document.source, profile=document.profile, max_bytes=max(1, len(document.raw)))
    if document != observed:
        return False
    _remember_source(document)
    return True


def _lines(value):
    pattern = _BYTE_LINE if isinstance(value, bytes) else _TEXT_LINE
    for match in checkpointed(pattern.finditer(value)):
        if match.start() != match.end():
            yield match.start(), match.end(), match.group()


def _splitlines(text):
    work_checkpoint()
    result = text.splitlines()
    work_checkpoint()
    return result


def _sha256(data):
    digest = hashlib.sha256()
    view = memoryview(data)
    for start in checkpointed(range(0, len(data), 1024*1024), interval=1):
        digest.update(view[start:start+1024*1024])
    return digest.hexdigest()


def _lookahead(lines):
    iterator = iter(lines)
    current = next(iterator, None)
    following = next(iterator, None)
    while current is not None:
        after = next(iterator, None)
        yield current, following, after
        current, following = following, after


@dataclass(frozen=True, kw_only=True)
class ReportBlock:
    title: str
    kind: str
    start_line: int
    end_line: int  # inclusive
    text: str
    target: Ref | None = None


@dataclass(frozen=True, kw_only=True)
class ReportMessage:
    """Printed diagnostic context; input coordinates are claims made by SWMM.

    ``input_report_span`` locates the echoed text in the RPT, not in the INP.
    Neither an object Ref nor a verified mapping to a captured INP is invented.
    """
    diagnostic: Diagnostic
    raw_text: str
    input_line: int | None = None
    input_section: str | None = None
    input_text: str | None = None
    input_report_span: SourceSpan | None = None
    model_time: datetime | None = None


def _messages(text,source):
    lines=iter(enumerate(_lines(text),1));result=[]
    pending=next(lines,None)
    while pending is not None:
        number,(_,_,raw)=pending
        pending=next(lines,None)
        line=raw.rstrip('\r\n')
        match=re.match(r'^\s*(ERROR|WARNING)\s+(\d+)(?::\s*(.*)| (detected\. Execution halted\.))\s*$',line)
        if not match:continue
        diagnostic=Diagnostic(code='swmm.'+match[1].lower()+'.'+match[2],message=match[3] or match[4] or match[0].strip(),
            severity=Severity.ERROR if match[1]=='ERROR' else Severity.WARNING,
            span=SourceSpan(line=number,column=match.start(1)+1,end_column=len(line)+1,source=source))
        where=re.search(r'at line ([1-9]\d*) of (?:input file|\[([^\]\r\n]+)\] section):\s*$',line)
        input_text=input_span=model_time=None
        # Native input errors and rain-sequence error 318 explicitly echo one
        # source line. Unknown diagnostics retain raw bytes without consuming
        # arbitrary following headings, warnings or diagnostic-looking text.
        if (where or (match[1]=='ERROR' and match[2]=='318')) and pending is not None and pending[1][2].startswith('  '):
            echo=pending[1][2].rstrip('\r\n');input_text=echo[2:]
            input_span=SourceSpan(line=pending[0],column=3,end_column=len(echo)+1,source=source)
            raw+=pending[1][2];pending=next(lines,None)
        moment=re.search(r' at (\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\.\s*$',line)
        if match[1]=='ERROR' and match[2]=='173' and moment:
            try:model_time=datetime.strptime(moment[1],'%m/%d/%Y %H:%M:%S')
            except ValueError:pass
        result.append(ReportMessage(diagnostic=diagnostic,raw_text=raw,input_line=int(where[1]) if where else None,
            input_section=where[2] if where else None,input_text=input_text,input_report_span=input_span,model_time=model_time))
    return tuple(result)


@dataclass(frozen=True, kw_only=True)
class ReportCapture:
    """Best-effort failed-run bytes, possibly a prefix; never a success artifact."""
    document: 'ReportDocument'
    truncated: bool

    @property
    def sha256(self):
        """Hash of captured bytes only, not of an unobserved remainder."""
        return hashlib.sha256(self.document.raw).hexdigest()

    @classmethod
    def read(cls,path,*,max_bytes=DEFAULT_REPORT_LIMIT,encoding=None,profile=None,checkpoint=None):
        with checkpoint_scope(checkpoint):
            return cls._read(path,max_bytes=max_bytes,encoding=encoding,profile=profile)

    @classmethod
    def _read(cls,path,*,max_bytes,encoding,profile):
        if type(max_bytes) is not int or max_bytes<=0:raise ValueError('Report byte limit must be a positive integer')
        path=Path(path)
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('Failure report capture requires a regular owned file')
        chunks=[];size=0
        with path.open('rb') as stream:
            while size<=max_bytes:
                work_checkpoint()
                chunk=stream.read(min(65536,max_bytes+1-size))
                if not chunk:break
                chunks.append(chunk);size+=len(chunk)
        data=b''.join(chunks)
        document=ReportDocument.from_bytes(data[:max_bytes],max_bytes=max_bytes,encoding=encoding,
            profile=profile,source=str(path),on_decode_error='preserve')
        return cls(document=document,truncated=size>max_bytes)


def _catalog(text):
    starts = []
    count = 0
    for number, (current, following, after) in enumerate(_lookahead(_lines(text)), 1):
        offset, _, line = current
        count = number
        if (after is not None and re.match(r'^\s*\*{3,}', line)
                and re.match(r'^\s*\*{3,}', after[2]) and not following[2].lstrip().startswith('*')):
            title = re.split(r'\s{2,}', following[2].strip())[0]
            starts.append((offset, number, title, 'section', None))
        match = re.fullmatch(r'\s*<<< (Subcatchment|Node|Link) (\S+) >>>\s*', line)
        if match:
            collection = {'Subcatchment': 'subcatchments', 'Node': 'nodes', 'Link': 'links'}[match[1]]
            starts.append((offset, number, match[1]+' '+match[2], 'detail', Ref(collection='swmm:'+collection, key=match[2])))
        if line.lstrip().startswith('Analysis begun on:'):
            starts.append((offset, number, 'Analysis timing', 'timing', None))
    if not starts or starts[0][0] != 0:
        starts.insert(0, (0, 1, 'Report preamble', 'preamble', None))
    return tuple(ReportBlock(title=title, kind=kind, target=target, start_line=number,
        end_line=end_number-1, text=text[start:end])
        for (start, number, title, kind, target), (end, end_number) in
        checkpointed(zip(starts, [(x[0], x[1]) for x in starts[1:]]+[(len(text), count+1)]))
        if start < end)


def _volume_headers(data):
    """Only repair the two native header grammars containing literal ANSI 0xB3.

    IDs, comments, other invalid bytes and UTF-8 continuation bytes are untouched.
    The caller must explicitly assert a producer using UTF-8 input identifiers.
    """
    repairs = []
    section = None
    previous = None
    for current, following, _ in _lookahead(_lines(data)):
        offset, _, line = current
        if previous is not None and following is not None and previous.strip().startswith(b'***') and following[2].strip().startswith(b'***'):
            section = line.strip()
        storage = (section == b'Storage Volume Summary' and re.fullmatch(
            rb'\s*Storage Unit\s+1000 (?:ft|m)\xb3\s+Full\s+Loss\s+Loss\s+1000 (?:ft|m)\xb3\s+Full\s+days hr:min\s+(?:CFS|GPM|MGD|CMS|LPS|MLD)\s*', line))
        flooding = (section == b'Node Flooding Summary' and re.fullmatch(
            rb'\s*Node\s+Flooded\s+(?:CFS|GPM|MGD|CMS|LPS|MLD)\s+days hr:min\s+10\^6 (?:gal|ltr)\s+1000 (?:ft|m)\xb3\s*', line))
        if storage or flooding:
            repairs.extend(offset+j for j, byte in enumerate(line) if byte == 0xB3)
        previous = line
    if not repairs:
        return data, ()
    chunks = []; start = 0; view = memoryview(data)
    for offset in repairs:
        chunks.extend((view[start:offset], b'\xc2\xb3'))
        start = offset+1
    chunks.append(view[start:])
    return b''.join(chunks), tuple(repairs)


@dataclass(frozen=True, kw_only=True)
class ReportDocument:
    raw: bytes
    text: str | None
    requested_encoding: str | None
    encoding: str | None
    diagnostics: ValidationReport
    source: str | None = None
    profile: str | None = None
    decoding_strategy: str = 'strict'
    repaired_byte_offsets: tuple[int, ...] = ()
    blocks: tuple[ReportBlock, ...] = ()
    messages: tuple[Diagnostic, ...] = ()
    message_contexts: tuple[ReportMessage, ...] = ()

    @classmethod
    def from_bytes(cls, data, *, encoding=None, on_decode_error='preserve', source=None,
                   profile=None, max_bytes=DEFAULT_REPORT_LIMIT, checkpoint=None):
        with checkpoint_scope(checkpoint):
            return cls._from_bytes(data,encoding=encoding,on_decode_error=on_decode_error,
                source=source,profile=profile,max_bytes=max_bytes)

    @classmethod
    def _from_bytes(cls, data, *, encoding, on_decode_error, source, profile, max_bytes):
        if type(data) is not bytes or on_decode_error not in ('preserve','raise'):
            raise ValueError('Expected immutable report bytes and an explicit decode policy')
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('Report byte limit must be a positive integer')
        if len(data) > max_bytes:
            raise ValueError('Report exceeds max_bytes; raise the explicit budget to read it')
        if profile not in (None, SWMM_UTF8_REPORT):
            raise ValueError('Unknown report decoding profile')
        candidate=codecs.lookup(encoding or ('utf-8-sig' if data.startswith(codecs.BOM_UTF8) else 'utf-8')).name
        repairs=(); strategy='strict'; issues=[]
        try:
            try:
                text=data.decode(candidate,errors='strict')
            except UnicodeDecodeError as original_error:
                if encoding is not None or profile != SWMM_UTF8_REPORT:
                    raise
                normalized, repairs = _volume_headers(data)
                try:
                    text=normalized.decode(candidate, errors='strict')
                except UnicodeDecodeError:
                    # Diagnostics refer to original byte offsets, never offsets
                    # displaced by the attempted header normalization.
                    raise original_error
                finally:
                    del normalized
                strategy='swmm-volume-header-normalization'
                issues.append(Diagnostic(code='report.volume_header_encoding', severity=Severity.INFO,
                    message=f'Normalized {len(repairs)} native ANSI superscripts in known volume headers; original bytes and offsets retained.'))
        except UnicodeDecodeError as error:
            if on_decode_error=='raise':raise
            document=cls(raw=data,text=None,requested_encoding=encoding,encoding=None,source=source,
                profile=profile, decoding_strategy='undecoded',
                diagnostics=ValidationReport(diagnostics=(Diagnostic(code='report.undecoded_bytes',severity=Severity.WARNING,
                    message=f'RPT is not valid {candidate} at byte {error.start}; original bytes retained. Specify an encoding supported by its producer.'),)))
            _remember_source(document)
            return document
        work_checkpoint()
        contexts=_messages(text,source)
        document=cls(raw=data,text=text,requested_encoding=encoding,encoding=candidate,source=source,
            profile=profile,decoding_strategy=strategy,repaired_byte_offsets=repairs,blocks=_catalog(text),
            messages=tuple(item.diagnostic for item in contexts),message_contexts=contexts,
            diagnostics=ValidationReport(diagnostics=tuple(issues)))
        _remember_source(document)
        return document

    @classmethod
    def read(cls,path,*,max_bytes=DEFAULT_REPORT_LIMIT,source=None,checkpoint=None,**options):
        with checkpoint_scope(checkpoint):
            return cls._read(path,max_bytes=max_bytes,source=source,**options)

    @classmethod
    def _read(cls,path,*,max_bytes,source,**options):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('Report byte limit must be a positive integer')
        # BufferedReader.read(large_limit) can allocate the entire requested
        # capacity even for a tiny file. Keep transient reads bounded as well.
        chunks=[];size=0
        with Path(path).open('rb') as stream:
            while True:
                work_checkpoint()
                chunk=stream.read(min(65536,max_bytes+1-size))
                if not chunk:break
                size+=len(chunk)
                if size>max_bytes:
                    raise ValueError('Report exceeds max_bytes; raise the explicit budget to read it')
                chunks.append(chunk)
        data=b''.join(chunks)
        del chunks,chunk
        return cls.from_bytes(data,source=str(path) if source is None else source,max_bytes=max_bytes,**options)
