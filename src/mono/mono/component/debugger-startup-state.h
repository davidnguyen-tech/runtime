// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

#ifndef MONO_DEBUGGER_STARTUP_STATE_H
#define MONO_DEBUGGER_STARTUP_STATE_H

#include <stdint.h>
#include <mono/utils/atomic.h>

enum { MONO_STARTUP_RECORD_BYTES = 2048, MONO_STARTUP_RECORDS = 256 };

typedef enum {
	MS_START, MS_HEALTH, MS_COMPONENT, MS_CONFIG, MS_FINISH, MS_TRANSPORT,
	MS_CREATE, MS_REUSEADDR, MS_NODELAY, MS_SNDTIMEO, MS_RCVTIMEO,
	MS_BIND, MS_LISTEN, MS_ENDPOINT, MS_WAIT, MS_ACCEPT, MS_CONNECT,
	MS_HANDSHAKE, MS_SHUT_RD, MS_SHUT_RDWR, MS_CLOSE
} MonoStartupEvent;

typedef enum { MS_BEGIN, MS_END, MS_INSTANT } MonoStartupPhase;
typedef enum { MS_UNKNOWN, MS_LISTENER, MS_CLIENT, MS_ACCEPTED, MS_INHERITED } MonoStartupRole;

typedef struct {
	uint64_t value;
	int valid;
} MonoStartupNumber;

typedef struct {
	MonoStartupNumber before, real, after;
} MonoStartupClock;

typedef struct {
	int pid;
	uint32_t uid;
	MonoStartupNumber start;
	char boot [37];
} MonoStartupIdentity;

typedef struct {
	int fd, family, port;
	MonoStartupRole role;
	int id, parent;
} MonoStartupSocket;

typedef struct {
	MonoStartupSocket socket;
	int result, error;
	int enabled, server, suspend, defer;
	int startup, already;
	int transport;
} MonoStartupData;

typedef struct {
	MonoStartupClock (*clock) (void);
	MonoStartupIdentity (*identity) (void);
	int (*tid) (void);
	int (*write) (const char *);
} MonoStartupPlatform;

typedef struct {
	volatile gint32 state;
	volatile gint64 attempted, accepted, failed, format_dropped, cap_dropped;
	volatile gint32 sockets;
	struct {
		volatile gint32 state;
		MonoStartupSocket value;
		volatile gint32 port;
	} socket [MONO_STARTUP_RECORDS];
	char capture [33], build [65];
	MonoStartupIdentity identity;
	const MonoStartupPlatform *platform;
} MonoStartupState;

int mono_startup_valid_token (const char *value);
int mono_startup_valid_build (const char *value);
int mono_startup_valid_boot (const char *value);
MonoStartupNumber mono_startup_parse_ticks (const char *stat, int pid);
MonoStartupNumber mono_startup_nanoseconds (int64_t seconds, int64_t nanos);
void mono_startup_initialize (MonoStartupState *state, const MonoStartupPlatform *platform, const char *token, const char *build);
void mono_startup_record (MonoStartupState *state, MonoStartupEvent event, MonoStartupPhase phase, MonoStartupData data);
MonoStartupSocket mono_startup_socket_identity (MonoStartupState *state, int fd);
MonoStartupSocket mono_startup_socket_created (MonoStartupState *state, int fd, int family, int port, MonoStartupRole role, int parent);
void mono_startup_socket_retired (MonoStartupState *state, MonoStartupSocket socket);
void mono_startup_socket_port (MonoStartupState *state, MonoStartupSocket socket, int port);

#endif
