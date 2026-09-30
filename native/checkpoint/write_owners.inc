/* Appended to swmm5.c. Stable roles, no captured physical output paths. */
int es_ck_write_inventory(EsCkWriteOwner *owners, uint32_t capacity, uint32_t *count)
{
    int role, active, error;
    TFile *file;
    EsCkWriteOwner *o;
    *count = 0;
    if (es_ck_engine_boundary() || es_ck_report_boundary()) return ES_CK_STATE;
    for (role=0; role<5; role++)
    {
        active = 1;
        switch (role)
        {
        case 0: file=&Frpt; break;
        case 1: file=&Fout; break;
        case 2: file=&Frunoff; active=DoRunoff && file->mode==SAVE_FILE; break;
        case 3: file=&Foutflows; active=DoRouting && file->mode==SAVE_FILE; break;
        default: file=&Fhotstart2; active=file->mode==SAVE_FILE; break;
        }
        if (!active) continue;
        if (owners)
        {
            if (*count>=capacity) return ES_CK_BOUNDS;
            o=&owners[*count]; memset(o,0,sizeof(*o));
            o->file=&file->file; o->fixed_name=file->name;
            o->name_capacity=sizeof(file->name);
            o->role=role; o->text=role==0 || role==3;
            o->identity=""; o->component="";
        }
        (*count)++;
    }
    error=es_ck_lid_write_inventory(owners,capacity,count);
    return error;
}
