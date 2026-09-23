// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

#include <config.h>
#include "debugger-startup.h"

#if (defined(HOST_ANDROID) && defined(MONO_ANDROID_STARTUP_BUILD_ID)) || defined(MONO_DEBUGGER_STARTUP_WRAPPER_TEST)
#ifndef MONO_DEBUGGER_STARTUP_WRAPPER_TEST
#include <android/log.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#endif

static MonoStartupState state;
static volatile gint32 selected_transport;

#ifndef MONO_DEBUGGER_STARTUP_WRAPPER_TEST
static MonoStartupNumber
read_clock (clockid_t id)
{
	struct timespec value;
	if (clock_gettime (id, &value) != 0)
		return (MonoStartupNumber) { 0, 0 };
	return mono_startup_nanoseconds (value.tv_sec, value.tv_nsec);
}

static MonoStartupClock
clock_sample (void)
{
	MonoStartupClock clock;
	clock.before = read_clock (CLOCK_MONOTONIC);
	clock.real = read_clock (CLOCK_REALTIME);
	clock.after = read_clock (CLOCK_MONOTONIC);
	return clock;
}

static int
read_identity_file (const char *path, char *buffer, size_t capacity)
{
	int fd = open (path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 0;
	ssize_t size = read (fd, buffer, capacity - 1);
	close (fd);
	if (size <= 0 || (size_t)size == capacity - 1) {
		buffer [0] = 0;
		return 0;
	}
	buffer [size] = 0;
	return 1;
}

static MonoStartupIdentity
identity (void)
{
	MonoStartupIdentity value = { 0 };
	char buffer [4096];
	value.pid = getpid ();
	value.uid = getuid ();
	if (read_identity_file ("/proc/sys/kernel/random/boot_id", buffer, 64)) {
		if (strlen (buffer) == 37 && buffer [36] == '\n')
			buffer [36] = 0;
		if (mono_startup_valid_boot (buffer))
			memcpy (value.boot, buffer, sizeof (value.boot));
	}
	if (read_identity_file ("/proc/self/stat", buffer, sizeof (buffer)))
		value.start = mono_startup_parse_ticks (buffer, value.pid);
	return value;
}

static int
thread_id (void)
{
	return gettid ();
}

static int
write_record (const char *record)
{
	return __android_log_write (ANDROID_LOG_INFO, "mono-startup-meta", record);
}

static const MonoStartupPlatform platform = { clock_sample, identity, thread_id, write_record };

void
mono_debugger_startup_init (void)
{
	int saved = errno;
	if (mono_atomic_load_i32 (&state.state) == 0)
		mono_startup_initialize (&state, &platform, getenv ("DOTNET_ANDROID_STARTUP_CAPTURE_ID"), MONO_ANDROID_STARTUP_BUILD_ID);
	errno = saved;
}
#endif

void
mono_debugger_startup_component (MonoStartupPhase phase, int enabled)
{
	MonoStartupData data = { 0 };
	data.enabled = enabled;
	mono_startup_record (&state, MS_COMPONENT, phase, data);
	if (phase == MS_END)
		mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
}

void
mono_debugger_startup_config (int enabled, int server, int suspend, int defer)
{
	MonoStartupData data = { 0 };
	data.enabled = enabled;
	data.server = server;
	data.suspend = suspend;
	data.defer = defer;
	mono_startup_record (&state, MS_CONFIG, MS_INSTANT, data);
}

void
mono_debugger_startup_finish (MonoStartupPhase phase, int startup, int already)
{
	MonoStartupData data = { 0 };
	data.startup = startup;
	data.already = already;
	mono_startup_record (&state, MS_FINISH, phase, data);
}

void
mono_debugger_startup_transport (int transport)
{
	MonoStartupData data = { 0 };
	data.transport = transport;
	mono_atomic_store_i32 (&selected_transport, transport);
	mono_startup_record (&state, MS_TRANSPORT, MS_INSTANT, data);
}

static void
socket_event (MonoStartupEvent event, MonoStartupPhase phase, MonoStartupSocket socket, int result, int error)
{
	MonoStartupData data = { 0 };
	data.socket = socket;
	data.result = result;
	data.error = error;
	mono_startup_record (&state, event, phase, data);
}

void
mono_debugger_startup_handshake (MonoStartupPhase phase, int fd, int result)
{
	MonoStartupSocket socket = mono_startup_socket_identity (&state, mono_atomic_load_i32 (&selected_transport) ? fd : -1);
	socket_event (MS_HANDSHAKE, phase, socket, result, 0);
}

void
mono_debugger_startup_inherited (int fd)
{
	mono_startup_socket_created (&state, fd, 0, -1, MS_INHERITED, 0);
}

static int
family_kind (int family)
{
	return family == AF_INET ? 4 : family == AF_INET6 ? 6 : -1;
}

int
mono_debugger_startup_socket (int family, int type, int protocol, MonoStartupRole role, int port)
{
	MonoStartupSocket metadata = { -1, family_kind (family), port, role, 0, 0 };
	socket_event (MS_CREATE, MS_BEGIN, metadata, 0, 0);
	int result = socket (family, type, protocol);
	int saved = errno;
	if (result >= 0)
		metadata = mono_startup_socket_created (&state, result, metadata.family, port, role, 0);
	socket_event (MS_CREATE, MS_END, metadata, result, result < 0 ? saved : 0);
	errno = saved;
	return result;
}

// Every wrapper performs the original operation exactly once. Snapshot identity
// before blocking; sample failure errno before any formatting, clocks or logging.
#define START_SOCKET_OPERATION(event) \
	MonoStartupSocket metadata = mono_startup_socket_identity (&state, fd); \
	socket_event (event, MS_BEGIN, metadata, 0, 0)
#define END_SOCKET_OPERATION(event) \
	int saved = errno; \
	socket_event (event, MS_END, metadata, result, result < 0 ? saved : 0); \
	errno = saved; \
	return result

int
mono_debugger_startup_setsockopt (int fd, int level, int name, const void *value, socklen_t length, MonoStartupEvent event)
{
	START_SOCKET_OPERATION (event);
	int result = setsockopt (fd, level, name, value, length);
	END_SOCKET_OPERATION (event);
}

int
mono_debugger_startup_bind (int fd, const struct sockaddr *address, socklen_t length)
{
	START_SOCKET_OPERATION (MS_BIND);
	int result = bind (fd, address, length);
	END_SOCKET_OPERATION (MS_BIND);
}

int
mono_debugger_startup_listen (int fd, int backlog)
{
	START_SOCKET_OPERATION (MS_LISTEN);
	int result = listen (fd, backlog);
	END_SOCKET_OPERATION (MS_LISTEN);
}

int
mono_debugger_startup_getsockname (int fd, struct sockaddr *address, socklen_t *length)
{
	START_SOCKET_OPERATION (MS_ENDPOINT);
	int result = getsockname (fd, address, length);
	int saved = errno;
	if (!result) {
		if (address->sa_family == AF_INET && *length >= sizeof (struct sockaddr_in))
			metadata.port = ntohs (((struct sockaddr_in *)address)->sin_port);
		else if (address->sa_family == AF_INET6 && *length >= sizeof (struct sockaddr_in6))
			metadata.port = ntohs (((struct sockaddr_in6 *)address)->sin6_port);
		mono_startup_socket_port (&state, metadata, metadata.port);
	}
	socket_event (MS_ENDPOINT, MS_END, metadata, result, result < 0 ? saved : 0);
	errno = saved;
	return result;
}

int
mono_debugger_startup_poll (mono_pollfd *fds, unsigned int count, int timeout)
{
	int fd = count == 1 ? fds [0].fd : -1;
	START_SOCKET_OPERATION (MS_WAIT);
	int result = mono_poll (fds, count, timeout);
	END_SOCKET_OPERATION (MS_WAIT);
}

int
mono_debugger_startup_accept (int fd)
{
	START_SOCKET_OPERATION (MS_ACCEPT);
	int result = accept (fd, NULL, NULL);
	int saved = errno;
	if (result >= 0)
		metadata = mono_startup_socket_created (&state, result, metadata.family, metadata.port, MS_ACCEPTED, metadata.id);
	socket_event (MS_ACCEPT, MS_END, metadata, result, result < 0 ? saved : 0);
	errno = saved;
	return result;
}

int
mono_debugger_startup_connect (int fd, const struct sockaddr *address, socklen_t length)
{
	START_SOCKET_OPERATION (MS_CONNECT);
	int result = connect (fd, address, length);
	END_SOCKET_OPERATION (MS_CONNECT);
}

int
mono_debugger_startup_shutdown (int fd, int how)
{
	MonoStartupEvent event = how == SHUT_RD ? MS_SHUT_RD : MS_SHUT_RDWR;
	START_SOCKET_OPERATION (event);
	int result = shutdown (fd, how);
	END_SOCKET_OPERATION (event);
}

int
mono_debugger_startup_close (int fd)
{
	START_SOCKET_OPERATION (MS_CLOSE);
	int result = close (fd);
	int saved = errno;
	if (!result)
		mono_startup_socket_retired (&state, metadata);
	socket_event (MS_CLOSE, MS_END, metadata, result, result < 0 ? saved : 0);
	errno = saved;
	return result;
}
#endif
