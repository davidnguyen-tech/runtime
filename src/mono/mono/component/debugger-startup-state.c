// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

#include <config.h>
#include "debugger-startup-state.h"
#include <errno.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static int
lower_hex (char c)
{
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

int
mono_startup_valid_token (const char *value)
{
	if (!value)
		return 0;
	for (int i = 0; i < 32; ++i)
		if (!lower_hex (value [i]))
			return 0;
	return value [32] == 0;
}

int
mono_startup_valid_build (const char *value)
{
	if (!value || !value [0])
		return 0;
	for (int i = 0; i <= 64; ++i) {
		char c = value [i];
		if (!c)
			return 1;
		if (i == 64 || !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
		                 (i && (c == '.' || c == '_' || c == '-'))))
			return 0;
	}
	return 0;
}

int
mono_startup_valid_boot (const char *value)
{
	for (int i = 0; i < 36; ++i) {
		if (i == 8 || i == 13 || i == 18 || i == 23) {
			if (value [i] != '-')
				return 0;
		} else if (!lower_hex (value [i])) {
			return 0;
		}
	}
	return value [36] == 0;
}

static MonoStartupNumber
unsigned_number (const char *begin, const char *end)
{
	MonoStartupNumber number = { 0, 0 };
	if (begin == end)
		return number;
	for (const char *p = begin; p < end; ++p) {
		if (*p < '0' || *p > '9' || number.value > (UINT64_MAX - (*p - '0')) / 10)
			return (MonoStartupNumber) { 0, 0 };
		number.value = number.value * 10 + (*p - '0');
	}
	number.valid = 1;
	return number;
}

MonoStartupNumber
mono_startup_parse_ticks (const char *stat, int pid)
{
	const char *space = strchr (stat, ' ');
	const char *comm = strrchr (stat, ')');
	MonoStartupNumber invalid = { 0, 0 };
	if (!space || !comm || space [1] != '(' || comm <= space + 1 || comm [1] != ' ')
		return invalid;
	MonoStartupNumber parsed_pid = unsigned_number (stat, space);
	if (!parsed_pid.valid || pid <= 0 || parsed_pid.value != (uint64_t)pid)
		return invalid;
	const char *p = comm + 2;
	// Match the Android producer: comm can contain spaces and closing parentheses.
	for (int field = 3; field <= 22; ++field) {
		const char *end = strchr (p, ' ');
		if (field == 22) {
			MonoStartupNumber result = unsigned_number (p, end ? end : p + strlen (p));
			return result.valid && result.value ? result : invalid;
		}
		if (!end || end == p)
			return invalid;
		p = end + 1;
	}
	return invalid;
}

MonoStartupNumber
mono_startup_nanoseconds (int64_t seconds, int64_t nanos)
{
	if (seconds < 0 || nanos < 0 || nanos >= 1000000000 ||
	    (uint64_t)seconds > (UINT64_MAX - (uint64_t)nanos) / 1000000000)
		return (MonoStartupNumber) { 0, 0 };
	return (MonoStartupNumber) { (uint64_t)seconds * 1000000000 + (uint64_t)nanos, 1 };
}

typedef struct {
	char text [MONO_STARTUP_RECORD_BYTES + 1];
	size_t length;
	int invalid;
} StartupJson;

// The generic Mono JSON/logging helpers accept arbitrary text and allocate. This
// writer only formats validated identity, numeric fields and closed enum literals.
static void
append (StartupJson *json, const char *format, ...)
{
	if (json->invalid)
		return;
	va_list args;
	va_start (args, format);
	int count = vsnprintf (json->text + json->length, sizeof (json->text) - json->length, format, args);
	va_end (args);
	if (count < 0 || (size_t)count >= sizeof (json->text) - json->length)
		json->invalid = 1;
	else
		json->length += count;
}

static void
number (StartupJson *json, MonoStartupNumber value)
{
	if (value.valid)
		append (json, "\"%" PRIu64 "\"", value.value);
	else
		append (json, "null");
}

static const char *const event_names [] = {
	"capture_start", "capture_health", "component_init", "agent_config", "finish_init", "transport_select",
	"socket_create", "socket_option", "socket_option", "socket_option", "socket_option",
	"bind", "listen", "endpoint_query", "wait", "accept", "connect", "handshake", "shutdown", "shutdown", "close"
};
static const char *const operations [] = {
	"create", "reuseaddr", "nodelay", "sndtimeo", "rcvtimeo", "bind", "listen",
	"getsockname", "poll", "accept", "connect", "handshake", "shutdown_rd", "shutdown_rdwr", "close"
};
static const char *const roles [] = { "unknown", "listener", "client", "accepted", "inherited" };
static const char *const phases [] = { "begin", "end", "instant" };

static const char *
boolean (int value)
{
	return value ? "true" : "false";
}

static void
emit (MonoStartupState *state, MonoStartupEvent event, MonoStartupPhase phase, MonoStartupData data,
      uint64_t seq, int cap, MonoStartupClock clock)
{
	gint64 attempted = mono_atomic_load_i64 (&state->attempted);
	gint64 accepted = mono_atomic_load_i64 (&state->accepted);
	gint64 failed = mono_atomic_load_i64 (&state->failed);
	gint64 format_dropped = mono_atomic_load_i64 (&state->format_dropped);
	gint64 cap_dropped = mono_atomic_load_i64 (&state->cap_dropped);
	int tid = state->platform->tid ();
	if ((unsigned)event > MS_CLOSE || (unsigned)phase > MS_INSTANT || state->identity.pid <= 0 || tid <= 0) {
		mono_atomic_inc_i64 (&state->format_dropped);
		return;
	}
	StartupJson json = { { 0 }, 0, 0 };
	int boot_ok = mono_startup_valid_boot (state->identity.boot);
	int start_ok = state->identity.start.valid && state->identity.start.value;
	int clock_ok = clock.before.valid && clock.real.valid && clock.after.valid && clock.before.value <= clock.after.value;
	append (&json, "{\"schema\":1,\"component\":\"runtime\",\"build_id\":\"%s\",\"capture_id\":\"%s\","
	        "\"pid\":%d,\"tid\":%d,\"uid\":%" PRIu32 ",\"process_start_ticks\":",
	        state->build, state->capture, state->identity.pid, tid, state->identity.uid);
	number (&json, (MonoStartupNumber) { state->identity.start.value, start_ok });
	append (&json, ",\"boot_id\":");
	if (boot_ok)
		append (&json, "\"%s\"", state->identity.boot);
	else
		append (&json, "null");
	append (&json, ",\"identity_status\":\"%s\",\"seq\":\"%" PRIu64 "\",\"mono_before_ns\":", boot_ok && start_ok ? "ok" : "partial", seq);
	number (&json, clock.before);
	append (&json, ",\"real_ns\":");
	number (&json, clock.real);
	append (&json, ",\"mono_after_ns\":");
	number (&json, clock.after);
	append (&json, ",\"clock_status\":\"%s\",\"event\":\"%s\",\"phase\":\"%s\",\"data\":{",
	        clock_ok ? "ok" : "partial", event_names [event], phases [phase]);
	if (event == MS_START || event == MS_HEALTH) {
		json.invalid |= phase != MS_INSTANT;
		append (&json, "\"reason\":\"%s\",\"attempted\":\"%" PRIi64 "\",\"writeAccepted\":\"%" PRIi64
		        "\",\"writeFailed\":\"%" PRIi64 "\",\"formatDropped\":\"%" PRIi64 "\",\"capDropped\":\"%" PRIi64 "\"",
		        cap ? "cap" : event == MS_START ? "start" : "phase",
		        attempted, accepted, failed, format_dropped, cap_dropped);
	} else if (event == MS_COMPONENT) {
		json.invalid |= phase == MS_INSTANT;
		append (&json, "\"enabled\":%s", phase == MS_BEGIN ? "null" : boolean (data.enabled));
	} else if (event == MS_CONFIG) {
		json.invalid |= phase != MS_INSTANT;
		append (&json, "\"enabled\":%s,\"server\":%s,\"suspend\":%s,\"defer\":%s",
		        boolean (data.enabled), boolean (data.server), boolean (data.suspend), boolean (data.defer));
	} else if (event == MS_FINISH) {
		json.invalid |= phase == MS_INSTANT;
		append (&json, "\"mode\":\"%s\",\"result\":%s", data.startup ? "startup" : "lazy",
		        phase == MS_BEGIN ? "null" : data.already ? "\"already_initialized\"" : "\"initialized\"");
	} else if (event == MS_TRANSPORT) {
		json.invalid |= phase != MS_INSTANT || data.transport < 0 || data.transport > 2;
		append (&json, "\"transport\":\"%s\"", data.transport == 1 ? "socket" : data.transport == 2 ? "socketfd" : "custom");
	} else {
		MonoStartupSocket s = data.socket;
		if ((unsigned)s.role > MS_INHERITED) {
			mono_atomic_inc_i64 (&state->format_dropped);
			return;
		}
		json.invalid |= phase == MS_INSTANT || s.fd < -1 || s.id < 0 || s.parent < 0 ||
		                (phase == MS_BEGIN && data.error) || (data.error && (data.result >= 0 || data.error < 0)) ||
		                (event == MS_HANDSHAKE && (data.error || (phase == MS_END && data.result != 0 && data.result != 1)));
		append (&json, "\"socket_role\":\"%s\",\"socket_id\":", roles [s.role]);
		number (&json, (MonoStartupNumber) { (uint64_t)s.id, s.id > 0 });
		append (&json, ",\"fd\":%d,\"parent_socket_id\":", s.fd);
		number (&json, (MonoStartupNumber) { (uint64_t)s.parent, s.parent > 0 });
		append (&json, ",\"family\":%s,\"port\":", s.family == 4 ? "\"ipv4\"" : s.family == 6 ? "\"ipv6\"" : s.family ? "\"other\"" : "null");
		if (s.port >= 0 && s.port <= 65535)
			append (&json, "%d", s.port);
		else
			append (&json, "null");
		append (&json, ",\"operation\":\"%s\",\"result\":", operations [event - MS_CREATE]);
		if (phase == MS_BEGIN)
			append (&json, "null");
		else
			append (&json, "%d", data.result);
		append (&json, ",\"error_valid\":%s,\"error_code\":", boolean (data.error));
		if (data.error)
			append (&json, "%d", data.error);
		else
			append (&json, "null");
	}
	append (&json, "}}");
	if (json.invalid)
		mono_atomic_inc_i64 (&state->format_dropped);
	else if (state->platform->write (json.text) > 0)
		mono_atomic_inc_i64 (&state->accepted);
	else
		mono_atomic_inc_i64 (&state->failed);
}

void
mono_startup_initialize (MonoStartupState *state, const MonoStartupPlatform *platform, const char *token, const char *build)
{
	int saved = errno;
	if (mono_atomic_cas_i32 (&state->state, 1, 0) == 0) {
		if (!mono_startup_valid_token (token) || !mono_startup_valid_build (build)) {
			mono_atomic_store_i32 (&state->state, 3);
		} else {
			state->platform = platform;
			memcpy (state->capture, token, 33);
			memcpy (state->build, build, strlen (build) + 1);
			mono_atomic_store_i64 (&state->attempted, 1);
			// Keep the first observation before any proc identity I/O.
			MonoStartupClock first = platform->clock ();
			state->identity = platform->identity ();
			emit (state, MS_START, MS_INSTANT, (MonoStartupData) { 0 }, 1, 0, first);
			mono_atomic_store_i32 (&state->state, 2);
		}
	}
	errno = saved;
}

void
mono_startup_record (MonoStartupState *state, MonoStartupEvent event, MonoStartupPhase phase, MonoStartupData data)
{
	int saved = errno;
	if (mono_atomic_load_i32 (&state->state) == 2) {
		uint64_t seq = (uint64_t)mono_atomic_inc_i64 (&state->attempted);
		if (seq > MONO_STARTUP_RECORDS) {
			mono_atomic_inc_i64 (&state->cap_dropped);
			if (seq == MONO_STARTUP_RECORDS + 1)
				emit (state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 }, seq, 1, state->platform->clock ());
		} else {
			emit (state, event, phase, data, seq, 0, state->platform->clock ());
		}
	}
	errno = saved;
}

MonoStartupSocket
mono_startup_socket_identity (MonoStartupState *state, int fd)
{
	MonoStartupSocket unknown = { fd, 0, -1, MS_UNKNOWN, 0, 0 };
	if (mono_atomic_load_i32 (&state->state) != 2 || fd < 0)
		return unknown;
	int count = mono_atomic_load_i32 (&state->sockets);
	if (count > MONO_STARTUP_RECORDS)
		count = MONO_STARTUP_RECORDS;
	for (int i = count - 1; i >= 0; --i) {
		int status = mono_atomic_load_i32 (&state->socket [i].state);
		if (status && state->socket [i].value.fd == fd) {
			if (status == 2)
				return unknown;
			MonoStartupSocket socket = state->socket [i].value;
			socket.port = mono_atomic_load_i32 (&state->socket [i].port);
			return socket;
		}
	}
	return unknown;
}

MonoStartupSocket
mono_startup_socket_created (MonoStartupState *state, int fd, int family, int port, MonoStartupRole role, int parent)
{
	MonoStartupSocket socket = { fd, family, port, role, 0, parent };
	if (mono_atomic_load_i32 (&state->state) != 2 || fd < 0 ||
	    mono_atomic_load_i64 (&state->attempted) > MONO_STARTUP_RECORDS)
		return socket;
	// Entries are immutable after publication and never reused; this bounds storage
	// and avoids making fd reuse look like the same descriptor-wrapper lifetime.
	int id = mono_atomic_inc_i32 (&state->sockets);
	if (id > 0 && id <= MONO_STARTUP_RECORDS) {
		socket.id = id;
		state->socket [id - 1].value = socket;
		mono_atomic_store_i32 (&state->socket [id - 1].port, port);
		mono_atomic_store_i32 (&state->socket [id - 1].state, 1);
	}
	return socket;
}

void
mono_startup_socket_retired (MonoStartupState *state, MonoStartupSocket socket)
{
	if (socket.id > 0 && socket.id <= MONO_STARTUP_RECORDS)
		mono_atomic_store_i32 (&state->socket [socket.id - 1].state, 2);
}

void
mono_startup_socket_port (MonoStartupState *state, MonoStartupSocket socket, int port)
{
	if (socket.id > 0 && socket.id <= MONO_STARTUP_RECORDS)
		mono_atomic_store_i32 (&state->socket [socket.id - 1].port, port);
}
