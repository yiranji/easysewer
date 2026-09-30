/* Finp is opened in binary mode so Windows cannot silently treat Ctrl+Z as
 * EOF. Read a complete physical record before letting either parser pass
 * inspect it. MAXLINE-1 content bytes, plus LF and NUL, fit the caller. */
static int es_inputReadLine(char *line)
{
    size_t length = 0, content;
    int ch, any = 0, overflow = 0, invalid = 0;
    while ((ch = fgetc(Finp.file)) != EOF)
    {
        any = 1;
        if (ch == '\n') break;
        if (ch == 0 || ch == 26) invalid = 1;
        if (length < MAXLINE) line[length++] = (char)ch;
        else overflow = 1;
    }
    line[length] = 0;
    if (ferror(Finp.file)) return -3;
    if (!any) return 0;
    content = length;
    if (content && line[content-1] == '\r') content--;
    if (invalid) return -2;
    if (overflow || content >= MAXLINE) return -1;
    length = content;
    if (ch == '\n') line[length++] = '\n';
    line[length] = 0;
    return 1;
}
