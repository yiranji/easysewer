"""Instrument the production bootstrap without replacing interpreter safeguards."""
import ast


def before_worker_main(command, hook):
    """Return a copied command that executes a test hook before its final main()."""
    if len(command) != 6 or command[1:5] != ['-I', '-B', '-u', '-c']:
        raise AssertionError('Unexpected isolated worker command')
    source = command[-1]
    body = ast.parse(source).body
    if (len(body) < 2 or not isinstance(body[-2], ast.ImportFrom) or
            body[-2].module != 'easysewer.runtime._solver_worker' or
            body[-2].level != 0 or len(body[-2].names) != 1 or
            body[-2].names[0].name != 'main' or body[-2].names[0].asname is not None):
        raise AssertionError('Unexpected worker entry-point import')
    final = body[-1]
    if (not isinstance(final, ast.Expr) or not isinstance(final.value, ast.Call) or
            not isinstance(final.value.func, ast.Name) or final.value.func.id != 'main' or
            final.value.args or final.value.keywords):
        raise AssertionError('Expected a terminal worker main() call')
    # AST column offsets count UTF-8 bytes, including a Unicode package root in
    # the original single-line bootstrap. Preserve every existing source byte.
    raw = source.encode('utf-8')
    lines = raw.splitlines(keepends=True)
    offset = sum(map(len, lines[:final.lineno - 1])) + final.col_offset
    result = list(command)
    result[-1] = (raw[:offset].decode('utf-8') + 'exec(' + repr(hook) + '); ' +
                  raw[offset:].decode('utf-8'))
    return result
