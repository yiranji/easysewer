/* Development checkpoint ABI. Not yet installed or advertised by a backend.
 * Use with the matching engine artifact and complete verified outer container.
 * All calls and callbacks are serialized with every other solver API call.
 * Callbacks are trusted: they must not reenter or mutate the solver.
 */
#ifndef EASYSEWER_CHECKPOINT_API_H
#define EASYSEWER_CHECKPOINT_API_H
#include <stddef.h>
#include <stdint.h>
#ifdef _WIN32
#define ES_CK_EXPORT __declspec(dllexport)
#else
#define ES_CK_EXPORT
#endif
#ifdef __cplusplus
extern "C" {
#endif

/* Providers return an EsCkError (0 success). No FILE pointer crosses the ABI.
 * Input paths name immutable verified resource copies. Output paths name
 * independent verified prefix copies, already created by the provider.
 * Both providers must NUL-terminate within capacity; they own path cleanup.
 */
typedef int (*EsCkInputPath)(void *, const char *, char *, size_t);
typedef int (*EsCkOutputPath)(void *, int, uint32_t, int, uint64_t, char *, size_t);

ES_CK_EXPORT int swmm_checkpointVersion(void);
/* binding points to exactly 32 bytes: the coordinator's complete execution
 * identity digest, covering model/config/profile/engine/input resources.
 * Capture accepts NULL data and zero capacity to measure. On insufficient
 * capacity *used reports required size; on any failure data is unchanged.
 */
ES_CK_EXPORT int swmm_checkpointCapture(const void *binding, void *data,
                                      size_t capacity, size_t *used);
ES_CK_EXPORT int swmm_checkpointValidate(const void *binding, const void *data, size_t size);
/* Validate every native owner before ANY provider call. Hold a private byte
 * copy through validation/staging/application. The outer coordinator validates
 * Python owners and prepares their trace before calling restore.
 * committed is set once application starts. Error + committed means a fatal
 * internal contract violation, NOT a recoverable rejection. cleanup_error is
 * independent, including retired-stream failures after successful application.
 */
ES_CK_EXPORT int swmm_checkpointRestore(const void *binding, const void *data, size_t size,
    EsCkInputPath input, EsCkOutputPath output, void *context, int *committed, int *cleanup_error);

ES_CK_EXPORT int swmm_checkpointOutputCount(uint32_t *count);
ES_CK_EXPORT int swmm_checkpointOutputInfo(uint32_t index, int *role, int *text,
    uint64_t *bytes, char *path, size_t capacity);
ES_CK_EXPORT int swmm_checkpointInputCount(uint32_t *count);
/* ABI/wire version 2 uses stable role/table-index identities, never paths.
 * Table groups are 0 curves, 1 time series, 2 private climate consumers.
 * Ordered model objects and complete immutable input contents must be bound
 * by the outer execution identity; these logical keys alone do not prove it.
 * path identifies its initial reconstructed location. After restore, providers
 * retain the immutable resource mapping; do not reread a retired input path.
 * A shared physical file may have several distinct consumer identities.
 */
ES_CK_EXPORT int swmm_checkpointInputInfo(uint32_t index, char *identity,
    size_t identity_capacity, char *path, size_t path_capacity);
#ifdef __cplusplus
}
#endif
#endif
