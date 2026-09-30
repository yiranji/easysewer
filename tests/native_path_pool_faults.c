/* Compile with -I pointing at the actual prepared solver tree. */
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int calls, fail_at, live;
static void *tracked_malloc(size_t size)
{
    void *p;
    if (++calls == fail_at) return NULL;
    p = malloc(size);
    if (p) live++;
    return p;
}
static void tracked_free(void *p)
{
    if (p) live--;
    free(p);
}
#define malloc tracked_malloc
#define free tracked_free
#include "mempool.c"
#undef malloc
#undef free

int main(void)
{
    int failure, cycle;
    char *p, *q;
    for (failure = 1; failure <= 3; failure++)
    {
        calls = 0; fail_at = failure;
        assert(AllocInit() == NULL);
        assert(live == 0);
        fail_at = 0;
        assert(AllocInit());
        p = Alloc(64000);
        assert(p);
        memset(p, 'a', 64000);
        assert(p[63999] == 'a');
        assert(Alloc(1));
        assert(p[63999] == 'a');
        AllocFreePool();
        AllocFreePool();
        assert(live == 0);
    }
    for (failure = 1; failure <= 2; failure++)
    {
        fail_at = 0;
        assert(AllocInit());
        p = Alloc(63996);
        assert(p);
        memset(p, 'b', 63996);
        calls = 0; fail_at = failure;
        assert(Alloc(16) == NULL);
        assert(live == 3);
        assert(p[63995] == 'b');
        fail_at = 0;
        q = Alloc(4);
        assert(q == p+63996);
        memset(q, 'c', 4);
        assert(Alloc(16));
        assert(p[63995] == 'b' && q[3] == 'c');
        AllocFreePool();
        assert(live == 0);
    }
    for (cycle = 0; cycle < 100; cycle++)
    {
        fail_at = 0;
        assert(AllocInit());
        p = Alloc(4);
        memcpy(p, "abc", 4);
        calls = 0; fail_at = 2;
        assert(AllocInit() == NULL);
        assert(live == 3);
        fail_at = 0;
        assert(Alloc(0) == NULL);
        assert(Alloc(-1) == NULL);
        assert(Alloc(64001) == NULL);
        assert(Alloc(LONG_MAX) == NULL);
        assert(Alloc(64000));
        assert(strcmp(p, "abc") == 0);
        AllocReset();
        assert(Alloc(64000));
        assert(Alloc(64000));
        AllocFreePool();
        assert(live == 0);
    }
    puts("pool: 3 initialization faults, 2 extension faults, 100 recovery cycles passed");
    return 0;
}
