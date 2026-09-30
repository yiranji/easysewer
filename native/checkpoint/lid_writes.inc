/* Appended to lid.c. IDs + within-group deployment ordinal bind each stream;
 * changing a workspace path does not change this logical ownership. */
int es_ck_lid_write_inventory(EsCkWriteOwner *owners, uint32_t capacity, uint32_t *count)
{
    int j;
    uint32_t ordinal;
    TLidList *item;
    TLidUnit *u;
    TLidRptFile *r;
    EsCkWriteOwner *o;
    if (LidReportFailed || LidReportOwnError || LidDirtyReports ||
        GroupCount!=Nobjects[SUBCATCH] || (GroupCount && !LidGroups)) return ES_CK_STATE;
    for (j=0; j<GroupCount; j++)
    {
        if (!LidGroups[j]) continue;
        ordinal=0;
        for (item=LidGroups[j]->lidList; item; item=item->nextLidUnit,ordinal++)
        {
            u=item->lidUnit;
            if (!u || u->lidIndex<0 || u->lidIndex>=LidCount || ordinal==UINT32_MAX)
                return ES_CK_STATE;
            r=u->rptFile;
            if (!r) continue;
            if (r->failed || r->dirty || r->dirtyNext || *count==UINT32_MAX)
                return ES_CK_STATE;
            if (owners)
            {
                if (*count>=capacity) return ES_CK_BOUNDS;
                o=&owners[*count]; memset(o,0,sizeof(*o));
                o->file=&r->file; o->dynamic_name=&r->name;
                o->name_capacity=MAXFNAME+1; o->role=5; o->text=1;
                /* Provider uses inventory position; semantic identities and
                 * deployment ordinal are independently checked on the wire. */
                o->ordinal=ordinal; o->identity=Subcatch[j].ID;
                o->component=LidProcs[u->lidIndex].ID;
            }
            (*count)++;
        }
    }
    return 0;
}
