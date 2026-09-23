// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

#include <config.h>
#include "debugger-startup-state.h"
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef NDEBUG
#error Startup metadata tests require enabled assertions
#endif
#ifdef HOST_WIN32
#include <windows.h>
#else
#include <pthread.h>
#endif

static const char token [] = "0123456789abcdef0123456789abcdef";
static const char marker [] = "runtime-4271-host-test";
void mono_startup_test_wrappers (void);
static char records [MONO_STARTUP_RECORDS + 1][MONO_STARTUP_RECORD_BYTES + 1];
static volatile gint32 writes, clocks, identities;
static int fail_write, fail_clock, fail_identity, bad_tid;

static MonoStartupClock
test_clock (void)
{
	mono_atomic_inc_i32 (&clocks);
	errno = EDOM;
	MonoStartupClock result = { { 100, 1 }, { UINT64_MAX, 1 }, { 110, 1 } };
	if (fail_clock)
		result.real.valid = 0;
	return result;
}

static MonoStartupIdentity
test_identity (void)
{
	assert (mono_atomic_load_i32 (&clocks) == 1);
	mono_atomic_inc_i32 (&identities);
	MonoStartupIdentity result = { 123, 10001, { UINT64_MAX, 1 }, "aaeea91a-0066-4fa3-8f62-e08d1efaa51f" };
	if (fail_identity) {
		result.start.valid = 0;
		result.boot [0] = 0;
	}
	errno = ERANGE;
	return result;
}

static int
test_tid (void)
{
	errno = EINVAL;
	return bad_tid ? 0 : 456;
}

static int
test_write (const char *record)
{
	int slot = mono_atomic_inc_i32 (&writes) - 1;
	assert (slot >= 0 && slot <= MONO_STARTUP_RECORDS);
	assert (strlen (record) <= MONO_STARTUP_RECORD_BYTES);
	strcpy (records [slot], record);
	errno = EIO;
	return fail_write ? -1 : 1;
}

static const MonoStartupPlatform platform = { test_clock, test_identity, test_tid, test_write };

static void
reset (MonoStartupState *state)
{
	memset (state, 0, sizeof (*state));
	memset (records, 0, sizeof (records));
	writes = clocks = identities = 0;
	fail_write = fail_clock = fail_identity = bad_tid = 0;
}

static void
initialize (MonoStartupState *state)
{
	errno = E2BIG;
	mono_startup_initialize (state, &platform, token, marker);
	assert (errno == E2BIG);
	assert (writes == 1 && clocks == 1 && identities == 1);
	assert (strstr (records [0], "\"seq\":\"1\""));
	assert (strstr (records [0], "\"writeAccepted\":\"0\""));
}

static void
test_gate (void)
{
	const char *invalid [] = { NULL, "", "0123456789abcdef0123456789abcde",
		"0123456789abcdef0123456789abcdef0", "0123456789abcdef0123456789abcdeF",
		"0123456789abcdef0123456789abcde\"", "0123456789abcdef0123456789abcdef\n",
		"0123456789abcdef0123456789abcdef\r", "0123456789abcdef0123456789abcdef\r\n" };
	MonoStartupState state;
	for (size_t i = 0; i < sizeof (invalid) / sizeof (invalid [0]); ++i) {
		reset (&state);
		mono_startup_initialize (&state, &platform, invalid [i], marker);
		mono_startup_record (&state, MS_COMPONENT, MS_BEGIN, (MonoStartupData) { 0 });
		assert (!writes && !clocks && !identities && !state.attempted);
		mono_startup_initialize (&state, &platform, token, marker);
		assert (!writes);
	}
	const char *bad_builds [] = { "", "-invalid", "secret\"marker", "UPPER", "valid\n", "valid\r", "valid\r\n",
		"01234567890123456789012345678901234567890123456789012345678901234" };
	for (size_t i = 0; i < sizeof (bad_builds) / sizeof (bad_builds [0]); ++i) {
		reset (&state);
		mono_startup_initialize (&state, &platform, token, bad_builds [i]);
		assert (!writes && !clocks && !identities);
	}
	reset (&state);
	initialize (&state);
	mono_startup_initialize (&state, &platform, "ffffffffffffffffffffffffffffffff", "another");
	assert (!strcmp (state.capture, token) && identities == 1 && clocks == 1);
	char maximum_marker [65];
	memset (maximum_marker, 'a', 64);
	maximum_marker [64] = 0;
	assert (mono_startup_valid_build (maximum_marker));
}

static void
test_identity_parsing (void)
{
	const char *stat = "123 (space ) and parens)) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 18446744073709551615 23";
	MonoStartupNumber value = mono_startup_parse_ticks (stat, 123);
	assert (value.valid && value.value == UINT64_MAX);
	assert (!mono_startup_parse_ticks (stat, 124).valid);
	assert (!mono_startup_parse_ticks ("123 (x) S 1", 123).valid);
	assert (!mono_startup_parse_ticks ("garbage", 123).valid);
	assert (!mono_startup_parse_ticks ("123 (x) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 18446744073709551616", 123).valid);
	assert (!mono_startup_valid_boot ("aaeea91a-0066-4fa3-8f62-e08d1efaa51Z"));
	assert (!mono_startup_nanoseconds (-1, 1).valid);
	assert (!mono_startup_nanoseconds (INT64_MAX, 0).valid);
	assert (!mono_startup_nanoseconds (0, 1000000000).valid);
	assert (mono_startup_nanoseconds (1, 2).value == 1000000002);
}

static void
test_outcomes (void)
{
	MonoStartupState state;
	reset (&state);
	initialize (&state);
	MonoStartupSocket listener = mono_startup_socket_created (&state, 65, 4, 52237, MS_LISTENER, 0);
	assert (listener.id == 1);
	MonoStartupData data = { 0 };
	data.socket = listener;
	data.result = -1;
	data.error = EADDRINUSE;
	errno = EADDRINUSE;
	mono_startup_record (&state, MS_LISTEN, MS_END, data);
	assert (errno == EADDRINUSE);
	assert (strstr (records [1], "\"event\":\"listen\"") && strstr (records [1], "\"error_valid\":true"));
	data.result = 0;
	data.error = 0;
	mono_startup_record (&state, MS_LISTEN, MS_END, data);
	assert (errno == EADDRINUSE && strstr (records [2], "\"error_code\":null"));
	MonoStartupSocket connection = mono_startup_socket_created (&state, 66, 4, 52237, MS_ACCEPTED, listener.id);
	assert (connection.id == 2 && connection.parent == listener.id);
	mono_startup_socket_retired (&state, listener);
	assert (!mono_startup_socket_identity (&state, 65).id);
	MonoStartupSocket reused = mono_startup_socket_created (&state, 65, 6, 52000, MS_CLIENT, 0);
	assert (reused.id == 3 && mono_startup_socket_identity (&state, 65).id == 3);
	mono_startup_socket_retired (&state, listener);
	assert (mono_startup_socket_identity (&state, 65).id == 3);
	mono_startup_socket_port (&state, reused, 52100);
	assert (mono_startup_socket_identity (&state, 65).port == 52100);
	assert (!mono_startup_socket_identity (&state, 999).id);
	data = (MonoStartupData) { 0 };
	mono_startup_record (&state, MS_COMPONENT, MS_END, data);
	assert (strstr (records [3], "\"enabled\":false"));
	mono_startup_record (&state, MS_FINISH, MS_BEGIN, data);
	assert (strstr (records [4], "\"mode\":\"lazy\",\"result\":null"));
	data.already = 1;
	mono_startup_record (&state, MS_FINISH, MS_END, data);
	assert (strstr (records [5], "\"already_initialized\""));
}

static void
test_loss (void)
{
	MonoStartupState state;
	reset (&state);
	fail_clock = fail_identity = fail_write = 1;
	initialize (&state);
	assert (state.failed == 1 && !state.accepted);
	assert (strstr (records [0], "\"identity_status\":\"partial\""));
	assert (strstr (records [0], "\"clock_status\":\"partial\""));
	assert (strstr (records [0], "\"process_start_ticks\":null"));
	fail_write = 0;
	bad_tid = 1;
	mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	assert (state.format_dropped == 1 && writes == 1);
	bad_tid = 0;
	MonoStartupData bad = { 0 };
	bad.socket.role = (MonoStartupRole)99;
	mono_startup_record (&state, MS_LISTEN, MS_END, bad);
	assert (state.format_dropped == 2 && writes == 1);
	mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	assert (strstr (records [1], "\"writeFailed\":\"1\"") && strstr (records [1], "\"formatDropped\":\"2\""));
	reset (&state);
	initialize (&state);
	for (int i = 1; i < MONO_STARTUP_RECORDS; ++i)
		mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	fail_write = 1;
	mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	assert (state.attempted == 257 && state.cap_dropped == 1 && state.failed == 1 && writes == 257);
	assert (strstr (records [256], "\"writeFailed\":\"0\""));
	mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	assert (writes == 257 && state.failed == 1);
}

#ifdef HOST_WIN32
static DWORD WINAPI
record_thread (LPVOID arg)
#else
static void *
record_thread (void *arg)
#endif
{
	for (int i = 0; i < 100; ++i) {
		errno = E2BIG;
		mono_startup_record ((MonoStartupState *)arg, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
		assert (errno == E2BIG);
	}
	return 0;
}

static void
test_concurrency (void)
{
	MonoStartupState state;
	reset (&state);
	initialize (&state);
#ifdef HOST_WIN32
	HANDLE threads [8];
	for (int i = 0; i < 8; ++i) {
		threads [i] = CreateThread (NULL, 0, record_thread, &state, 0, NULL);
		assert (threads [i]);
	}
	assert (WaitForMultipleObjects (8, threads, TRUE, INFINITE) == WAIT_OBJECT_0);
	for (int i = 0; i < 8; ++i)
		CloseHandle (threads [i]);
#else
	pthread_t threads [8];
	for (int i = 0; i < 8; ++i)
		assert (!pthread_create (&threads [i], NULL, record_thread, &state));
	for (int i = 0; i < 8; ++i)
		assert (!pthread_join (threads [i], NULL));
#endif
	assert (state.attempted == 801 && state.cap_dropped == 545);
	assert (writes == 257 && clocks == 257 && state.accepted == 257);
	int seen [258] = { 0 };
	for (int i = 0; i < writes; ++i) {
		const char *seq = strstr (records [i], "\"seq\":\"");
		assert (seq);
		int ordinal = atoi (seq + 7);
		assert (ordinal > 0 && ordinal <= 257 && !seen [ordinal]);
		seen [ordinal] = 1;
		if (ordinal == 257)
			assert (strstr (records [i], "\"reason\":\"cap\""));
	}
	int previous_clocks = clocks;
	mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	assert (clocks == previous_clocks && writes == 257 && state.cap_dropped == 546);
}

static void
write_fixtures (const char *path)
{
	MonoStartupState state;
	reset (&state);
	initialize (&state);
	MonoStartupData data = { 0 };
	data.socket = mono_startup_socket_created (&state, 65, 4, 52237, MS_LISTENER, 0);
	for (int event = MS_COMPONENT; event <= MS_CLOSE; ++event) {
		if (event == MS_CONFIG || event == MS_TRANSPORT) {
			mono_startup_record (&state, (MonoStartupEvent)event, MS_INSTANT, data);
		} else {
			mono_startup_record (&state, (MonoStartupEvent)event, MS_BEGIN, data);
			mono_startup_record (&state, (MonoStartupEvent)event, MS_END, data);
		}
	}
	data.result = -1;
	data.error = EADDRINUSE;
	mono_startup_record (&state, MS_BIND, MS_END, data);
	mono_startup_record (&state, MS_HEALTH, MS_INSTANT, (MonoStartupData) { 0 });
	assert (!state.format_dropped);
	FILE *file = fopen (path, "wb");
	assert (file);
	for (int i = 0; i < writes; ++i)
		assert (fprintf (file, "%s\n", records [i]) > 0);
	assert (!fclose (file));
}

int
main (int argc, char **argv)
{
	test_gate ();
	test_identity_parsing ();
	test_outcomes ();
	test_loss ();
	test_concurrency ();
	mono_startup_test_wrappers ();
	if (argc == 2)
		write_fixtures (argv [1]);
	puts ("PASS: gate, identity/clock ordering, outcomes/FD reuse/late init, capture failure, bounds/cap/concurrency");
	return 0;
}
