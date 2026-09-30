/* Appended to climate.c. Independent series cursors and files have other owners. */
#include "es_checkpoint.h"

#define ES_CF(object, field) es_ck_fixed_f64(c, (object).field)
#define ES_CI(object, field) es_ck_fixed_i32(c, (object).field)
#define ES_CD(object, field) es_ck_f64(c, &(object).field)

void es_ck_climate(EsCkCursor *c)
{
    int i, j, count, front, year, month, day, last_day, elapsed;
    int eof, have_row, row_year, row_month, last_month;
    double last_state;
    es_ck_tag(c, "ESCLM001", 8);
    es_ck_fixed_i32(c, UnitSystem);
    es_ck_fixed_i32(c, IgnoreSnowmelt);
    es_ck_fixed_i32(c, Fclimate.mode);
    ES_CI(Temp, dataSource); ES_CI(Temp, tSeries);
    /* The baseline sets fileStartDate only when parsing TEMPERATURE FILE.
     * Without a climate input file it is unused and can belong to a previous
     * project. Bind a canonical inactive value, never that stale field. */
    es_ck_fixed_f64(c, Fclimate.mode == USE_FILE ? Temp.fileStartDate : 0.0);
    ES_CF(Temp, elev); ES_CF(Temp, anglat); ES_CF(Temp, dtlong);
    ES_CF(Temp, gamma); ES_CF(Temp, tanAnglat);
    ES_CI(Wind, type);
    ES_CI(Evap, type); ES_CI(Evap, tSeries); ES_CI(Evap, recoveryPattern); ES_CI(Evap, dryOnly);
    ES_CF(Snow, snotmp); ES_CF(Snow, tipm); ES_CF(Snow, rnm);
    for (i = 0; i < 2; i++) for (j = 0; j < 10; j++) es_ck_fixed_f64(c, Snow.adc[i][j]);
    for (i = 0; i < 12; i++)
    {
        es_ck_fixed_f64(c, Wind.aws[i]);
        es_ck_fixed_f64(c, Evap.monthlyEvap[i]); es_ck_fixed_f64(c, Evap.panCoeff[i]);
        es_ck_fixed_f64(c, Adjust.temp[i]); es_ck_fixed_f64(c, Adjust.evap[i]);
        es_ck_fixed_f64(c, Adjust.rain[i]); es_ck_fixed_f64(c, Adjust.hydcon[i]);
    }
    ES_CD(Temp, ta); ES_CD(Temp, tmax); ES_CD(Temp, ea); ES_CD(Wind, ws);
    ES_CD(Evap, rate); ES_CD(Evap, recoveryFactor);
    ES_CD(Snow, season); ES_CD(Snow, removed);
    ES_CD(Adjust, rainFactor); ES_CD(Adjust, hydconFactor);
    es_ck_f64(c, &LastDay); es_ck_f64(c, &NextEvapDate);
    es_ck_f64(c, &NextEvapRate); es_ck_f64(c, &ClEvapRate);
    last_state = ClLastState;
    last_state = es_ck_f64_value(c, &last_state);
    if (last_state != NO_DATE && (last_state < StartDateTime || last_state > EndDateTime))
        es_ck_fail(c, ES_CK_NUMBER);
    if (!c->error && c->mode == ES_CK_APPLY) ClLastState = last_state;
    if (Temp.dataSource == FILE_TEMP)
    {
        es_ck_f64(c, &Tmin); es_ck_f64(c, &Tmax); es_ck_f64(c, &Trng);
        es_ck_f64(c, &Trng1); es_ck_f64(c, &Tave); es_ck_f64(c, &Hrsr);
        es_ck_f64(c, &Hrss); es_ck_f64(c, &Hrday); es_ck_f64(c, &Dhrdy); es_ck_f64(c, &Dydif);
    }
    if (Evap.type == TEMPERATURE_EVAP)
    {
        es_ck_tag(c, "ESCMA001", 8);
        if (Tma.maxCount != 7) { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_fixed_i32(c, Tma.maxCount);
        count = Tma.count; front = Tma.front;
        count = es_ck_i32(c, &count, 0, 7);
        front = es_ck_i32(c, &front, 0, 6);
        if (count < 7 && front != count) es_ck_fail(c, ES_CK_NUMBER);
        es_ck_f64(c, &Tma.tAve); es_ck_f64(c, &Tma.tRng);
        /* Unfilled slots are stale from an earlier project; never inspect them. */
        for (i = 0; i < count && !c->error; i++)
        { es_ck_f64(c, &Tma.ta[i]); es_ck_f64(c, &Tma.tr[i]); }
        if (!c->error && c->mode == ES_CK_APPLY) { Tma.count = count; Tma.front = front; }
    }
    if (Fclimate.mode == USE_FILE)
    {
        es_ck_tag(c, "ESCLF001", 8);
        if (ClPending || ((c->mode == ES_CK_MEASURE || c->mode == ES_CK_WRITE) && !Fclimate.file))
        { es_ck_fail(c, ES_CK_STATE); return; }
        es_ck_identity(c, c->logical_resources ? es_ck_stream_key(0) : Fclimate.name);
        es_ck_fixed_i32(c, FileFormat);
        if (FileFormat == GHCND)
        {
            es_ck_fixed_i32(c, FileWindType); es_ck_fixed_i32(c, FileTempUnits);
            for (i = 0; i < 6; i++)
            { es_ck_fixed_i32(c, ClColumns[i]); es_ck_fixed_i32(c, ClEnds[i]); }
        }
        year = FileYear; month = FileMonth; day = FileDay;
        last_day = FileLastDay; elapsed = FileElapsedDays;
        year = es_ck_i32(c, &year, 1, 9999); month = es_ck_i32(c, &month, 1, 12);
        day = es_ck_i32(c, &day, 1, 31); last_day = es_ck_i32(c, &last_day, 28, 31);
        elapsed = es_ck_i32(c, &elapsed, 0, INT_MAX);
        eof = ClEof; have_row = ClHaveRow; last_month = ClLastMonth;
        row_year = ClRowYear; row_month = ClRowMonth;
        eof = es_ck_i32(c, &eof, 0, 1); have_row = es_ck_i32(c, &have_row, 0, 1);
        last_month = es_ck_i32(c, &last_month, 13, 120000);
        row_year = es_ck_i32(c, &row_year, 1, 9999); row_month = es_ck_i32(c, &row_month, 1, 12);
        if (!c->error && (!clDate(year, month, day) ||
            last_day != datetime_daysPerMonth(year, month) || (eof && have_row) ||
            last_month != row_year * 12 + row_month)) es_ck_fail(c, ES_CK_NUMBER);
        if (!c->error && datetime_encodeDate(year, month, day) -
            floor(Temp.fileStartDate == NO_DATE ? StartDate : Temp.fileStartDate) != elapsed)
            es_ck_fail(c, ES_CK_NUMBER);
        for (i = 0; i < 4 && !c->error; i++)
        {
            es_ck_f64(c, &FileValue[i]);
            for (j = 0; j < 32; j++)
            { es_ck_f64(c, &FileData[i][j]); es_ck_f64(c, &ClValues[i][j]); }
        }
        if (!c->error && c->mode == ES_CK_APPLY)
        {
            FileYear = year; FileMonth = month; FileDay = day;
            FileLastDay = last_day; FileElapsedDays = elapsed;
            ClEof = eof; ClHaveRow = have_row; ClLastMonth = last_month;
            ClRowYear = row_year; ClRowMonth = row_month;
        }
        /* ClPending is consumed by clStart before an engine capture boundary.
           FileLine is then scratch, replaced before each next parsed row.
           ClValues, however, can contain the next month's already parsed row. */
    }
    /* ClTempSeries/ClEvapSeries each own a TTable cursor; a later table/resource
       owner must stage and restore them, including their independent streams. */
}

#undef ES_CF
#undef ES_CI
#undef ES_CD
