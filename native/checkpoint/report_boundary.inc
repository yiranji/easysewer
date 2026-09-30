/* Appended to report.c. Successful API boundaries have drained this writer. */
#include "es_checkpoint.h"
int es_ck_report_boundary(void)
{
    return RptFailed || RptOwnError || RptDirty ? ES_CK_STATE : 0;
}
