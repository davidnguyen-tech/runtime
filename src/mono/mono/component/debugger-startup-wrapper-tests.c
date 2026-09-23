// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

#include <config.h>
#include "debugger-startup-state.h"
#include <assert.h>
#include <errno.h>
#include <string.h>

// Compile the production wrappers unchanged with deterministic syscall outcomes.
// This does not open sockets, perform protocol probes, or simulate an Android run.
#define MONO_DEBUGGER_STARTUP_WRAPPER_TEST
#define sockaddr StartupTestAddress
#define sockaddr_in StartupTestAddress4
#define sockaddr_in6 StartupTestAddress6
#define AF_INET 2
#define AF_INET6 10
#define SHUT_RD 0
#define SHUT_RDWR 2
typedef unsigned int socklen_t;
struct sockaddr { int sa_family; };
struct sockaddr_in { int sin_family; unsigned short sin_port; };
struct sockaddr_in6 { int sin6_family; unsigned short sin6_port; };
typedef struct { int fd; } mono_pollfd;

static int calls, operation_result, operation_error, write_result = 1;
static char last_record [MONO_STARTUP_RECORD_BYTES + 1];
void mono_startup_test_wrappers (void);

static int
operation (void)
{
	++calls;
	if (operation_result < 0)
		errno = operation_error;
	return operation_result;
}

static int test_socket (int family, int type, int protocol) { return operation (); }
static int test_option (int fd, int level, int name, const void *value, socklen_t size) { return operation (); }
static int test_address (int fd, const struct sockaddr *address, socklen_t size) { return operation (); }
static int test_two (int fd, int argument) { return operation (); }
static int test_poll (mono_pollfd *fds, unsigned int count, int timeout) { return operation (); }
static int test_accept (int fd, void *address, void *size) { return operation (); }
static int test_close (int fd) { return operation (); }
static int
test_endpoint (int fd, struct sockaddr *address, socklen_t *size)
{
	struct sockaddr_in *v4 = (struct sockaddr_in *)address;
	v4->sin_family = AF_INET;
	v4->sin_port = 52237;
	*size = sizeof (*v4);
	return operation ();
}

#define socket(...) test_socket (__VA_ARGS__)
#define setsockopt(...) test_option (__VA_ARGS__)
#define bind(...) test_address (__VA_ARGS__)
#define connect(...) test_address (__VA_ARGS__)
#define listen(...) test_two (__VA_ARGS__)
#define shutdown(...) test_two (__VA_ARGS__)
#define mono_poll(...) test_poll (__VA_ARGS__)
#define accept(...) test_accept (__VA_ARGS__)
#define close(...) test_close (__VA_ARGS__)
#define getsockname(...) test_endpoint (__VA_ARGS__)
#define ntohs(port) (port)
#include "debugger-startup.c"

static MonoStartupClock
wrapper_clock (void)
{
	errno = EIO;
	return (MonoStartupClock) { { 1, 1 }, { 2, 1 }, { 3, 1 } };
}

static MonoStartupIdentity
wrapper_identity (void)
{
	return (MonoStartupIdentity) { 123, 10001, { 1, 1 }, "aaeea91a-0066-4fa3-8f62-e08d1efaa51f" };
}

static int wrapper_tid (void) { errno = EIO; return 456; }
static int
wrapper_write (const char *record)
{
	strcpy (last_record, record);
	errno = EIO;
	return write_result;
}

void
mono_startup_test_wrappers (void)
{
	const MonoStartupPlatform os = { wrapper_clock, wrapper_identity, wrapper_tid, wrapper_write };
	memset (&state, 0, sizeof (state));
	operation_result = 7;
	errno = E2BIG;
	assert (mono_debugger_startup_socket (AF_INET, 1, 0, MS_LISTENER, 52237) == 7);
	assert (errno == E2BIG && calls == 1 && !state.attempted);
	mono_startup_initialize (&state, &os, "0123456789abcdef0123456789abcdef", "runtime-4271-wrapper-test");
	mono_debugger_startup_component (MS_BEGIN, 1);
	mono_debugger_startup_config (1, 1, 0, 1);
	mono_debugger_startup_transport (1);
	mono_debugger_startup_finish (MS_BEGIN, 0, 0);
	assert (mono_debugger_startup_socket (AF_INET, 1, 0, MS_LISTENER, 52237) == 7);
	assert (mono_startup_socket_identity (&state, 7).id == 1);
	operation_result = -1;
	operation_error = EADDRINUSE;
	int before = calls;
	assert (mono_debugger_startup_listen (7, 16) == -1);
	assert (calls == before + 1 && errno == EADDRINUSE);
	assert (strstr (last_record, "\"event\":\"listen\"") && strstr (last_record, "\"error_valid\":true"));
	operation_result = 0;
	before = calls;
	write_result = -1;
	assert (!mono_debugger_startup_listen (7, 16));
	assert (calls == before + 1 && errno == EADDRINUSE);
	assert (strstr (last_record, "\"error_code\":null") && state.failed == 2);
	write_result = 1;
	assert (!mono_debugger_startup_setsockopt (7, 1, 1, NULL, 0, MS_REUSEADDR));
	assert (!mono_debugger_startup_bind (7, NULL, 0));
	struct sockaddr_in addr;
	socklen_t length = sizeof (addr);
	assert (!mono_debugger_startup_getsockname (7, (struct sockaddr *)&addr, &length));
	assert (mono_startup_socket_identity (&state, 7).port == 52237);
	mono_pollfd pollfd = { 7 };
	assert (!mono_debugger_startup_poll (&pollfd, 1, 3000));
	operation_result = 8;
	assert (mono_debugger_startup_accept (7) == 8);
	assert (mono_startup_socket_identity (&state, 8).parent == 1);
	assert (mono_startup_socket_identity (&state, 8).id == 2);
	mono_debugger_startup_handshake (MS_BEGIN, 8, 0);
	mono_debugger_startup_handshake (MS_END, 8, 0);
	assert (strstr (last_record, "\"result\":0,\"error_valid\":false"));
	operation_result = 0;
	assert (!mono_debugger_startup_connect (8, NULL, 0));
	assert (!mono_debugger_startup_shutdown (8, SHUT_RDWR));
	assert (!mono_debugger_startup_close (7));
	assert (!mono_startup_socket_identity (&state, 7).id);
	operation_result = 7;
	assert (mono_debugger_startup_socket (AF_INET6, 1, 0, MS_CLIENT, 52238) == 7);
	assert (mono_startup_socket_identity (&state, 7).id == 3);
	mono_debugger_startup_inherited (9);
	assert (mono_startup_socket_identity (&state, 9).role == MS_INHERITED);
	mono_debugger_startup_finish (MS_END, 0, 1);
	assert (strstr (last_record, "\"mode\":\"lazy\",\"result\":\"already_initialized\""));
	assert (!state.format_dropped && errno == EADDRINUSE);
}
