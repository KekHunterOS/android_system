#ifndef NETD_RESOLV_STATIC_H
#define NETD_RESOLV_STATIC_H

#include <netdb.h>
#include <stdio.h>

/* this structure contains all the variables that were declared
 * 'static' in the original NetBSD resolver code.
 *
 * this caused vast amounts of crashes and memory corruptions
 * when the resolver was being used by multiple threads.
 *
 * (note: the OpenBSD/FreeBSD resolver has similar 'issues')
 */

#define MAXALIASES 35
#define MAXADDRS 35

struct res_static {
    char* h_addr_ptrs[MAXADDRS + 1];
    char* host_aliases[MAXALIASES];
    char hostbuf[8 * 1024];
    u_int32_t host_addr[16 / sizeof(u_int32_t)]; /* IPv4 or IPv6 */
    FILE* hostf;
    int stayopen;
    const char* servent_ptr;
    struct servent servent;
    struct hostent host;
};

res_static* res_get_static();

#endif  // NETD_RESOLV_STATIC_H
