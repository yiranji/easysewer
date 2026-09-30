"""Native resource ownership corrections; simulation equations are unchanged."""

from hotstart import once


def patch_lifecycle(contents):
    name = 'src/solver/swmm5.c'
    text = contents[name].decode()
    begin = text.index('int DLLEXPORT swmm_close()')
    end = text.index('int  DLLEXPORT swmm_getMassBalErr', begin)
    text = text[:begin] + CLOSE + text[end:]
    contents[name] = text.encode()
    # Runoff owns the normal end-of-run close; swmm_close also handles models
    # without runoff and failures before runoff starts. Clear shared ownership.
    name = 'src/solver/runoff.c'
    text = contents[name].decode()
    text = once(text, '    if ( Fclimate.file ) fclose(Fclimate.file);',
        '    if ( Fclimate.file ) fclose(Fclimate.file);\n'
        '    Fclimate.file = NULL;')
    contents[name] = text.encode()
    name = 'src/solver/treatmnt.c'
    text = contents[name].decode()
    text = once(text, '        Node[j].treatment[p].treatType = k;',
        '        mathexpr_delete(Node[j].treatment[p].equation);\n'
        '        Node[j].treatment[p].treatType = k;')
    contents[name] = text.encode()


CLOSE = r'''int DLLEXPORT swmm_close()
{
    // End even a partially started simulation before deleting model objects.
    if (IsStartedFlag) swmm_end();
    if (Fout.file) output_close();
    if (IsOpenFlag) project_close();
    report_writeSysTime();
    if (Fclimate.file)
    {
        fclose(Fclimate.file);
        Fclimate.file = NULL;
    }
    if (Finp.file)
    {
        fclose(Finp.file);
        Finp.file = NULL;
    }
    if (Frpt.file)
    {
        fclose(Frpt.file);
        Frpt.file = NULL;
    }
    if (Fout.file)
    {
        fclose(Fout.file);
        Fout.file = NULL;
        if (Fout.mode == SCRATCH_FILE) remove(Fout.name);
    }
    IsOpenFlag = FALSE;
    IsStartedFlag = FALSE;
    return 0;
}

'''
