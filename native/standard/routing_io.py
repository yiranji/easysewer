"""Complete routing interface frames and checked native output ownership."""

from pathlib import Path
from hotstart import once
from rainfall import replace_function


def patch_routing(contents):
    name='src/solver/iface.c';text=contents[name].decode('utf-8')
    text=once(text,'#include <stdlib.h>',
        '#include <stdlib.h>\n#include <stdint.h>\n#include <limits.h>\n#include <float.h>\n'
        '#include <errno.h>\n#include <ctype.h>\n#include <stdarg.h>\n#include <math.h>\n'
        '#include <sys/stat.h>\n#ifdef _WIN32\n#include <windows.h>\n#include <io.h>\n'
        '#else\n#include <unistd.h>\n#endif')
    text=replace_function(text,'void iface_openRoutingFiles()\n','int  isOutletNode(int i)\n',
        Path(__file__).with_suffix('.c').read_text(encoding='utf-8'))
    contents[name]=text.encode('utf-8')
    name='src/solver/project.c';text=contents[name].decode('utf-8')
    text=once(text,'#include <stdlib.h>', '#include <stdlib.h>\n#include <stdint.h>')
    text=once(text,'    size_t size = (size_t)nrows * (size_t)ncols;',
        '''    size_t size;
    if (nrows < 1 || ncols < 1 || (size_t)nrows > SIZE_MAX / sizeof(double*) ||
        (size_t)nrows > SIZE_MAX / sizeof(double) / (size_t)ncols) return NULL;
    size = (size_t)nrows * (size_t)ncols;''')
    text=once(text,'    memset(a[0], 0, size);','    memset(a[0], 0, size * sizeof(double));')
    contents[name]=text.encode('utf-8')
    name='src/solver/routing.c';text=contents[name].decode('utf-8')
    text=once(text,'    iface_openRoutingFiles();',
        '    iface_openRoutingFiles();\n    if (ErrorCode) return ErrorCode;')
    contents[name]=text.encode('utf-8')
    name='src/solver/error.h';text=contents[name].decode('utf-8')
    text=once(text,'      ERR_ROUTING_FILE_OPEN    = 351,',
        '      ERR_ROUTING_FILE_OPEN    = 351,\n      ERR_ROUTING_FILE_READ    = 352,\n'
        '      ERR_ROUTING_FILE_WRITE   = 354,')
    contents[name]=text.encode('utf-8')
    name='src/solver/error.txt';text=contents[name].decode('utf-8')
    text=once(text,'ERR(351,"\\n  ERROR 351: cannot open routing interface file %s.")',
        'ERR(351,"\\n  ERROR 351: cannot open routing interface file %s.")\n'
        'ERR(352,"\\n  ERROR 352: error reading routing interface file %s.")\n'
        'ERR(354,"\\n  ERROR 354: error writing routing interface file %s.")')
    contents[name]=text.encode('utf-8')
    name='src/solver/swmm5.c';text=contents[name].decode('utf-8')
    text+='\nint DLLEXPORT swmm_getEasySewerRoutingIO(void)\n{\n    return 1;\n}\n'
    contents[name]=text.encode('utf-8')
