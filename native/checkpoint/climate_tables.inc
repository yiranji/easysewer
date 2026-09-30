/* Appended to climate.c. Inactive consumer structs can retain old borrowed
 * entry pointers: expose only consumers rebuilt by this project's start. */
TTable *es_ck_climate_table(int consumer)
{
    if (consumer == 0 && Temp.dataSource == TSERIES_TEMP && Temp.tSeries >= 0)
        return &ClTempSeries;
    if (consumer == 1 && Evap.type == TIMESERIES_EVAP && Evap.tSeries >= 0)
        return &ClEvapSeries;
    return NULL;
}
