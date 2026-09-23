// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

#ifndef MONO_DEBUGGER_STARTUP_H
#define MONO_DEBUGGER_STARTUP_H

#if (defined(HOST_ANDROID) && defined(MONO_ANDROID_STARTUP_BUILD_ID)) || defined(MONO_DEBUGGER_STARTUP_WRAPPER_TEST)
#include "debugger-startup-state.h"
#ifndef MONO_DEBUGGER_STARTUP_WRAPPER_TEST
#include "debugger-poll.h"
#include <sys/socket.h>
#endif

void mono_debugger_startup_init (void);
void mono_debugger_startup_component (MonoStartupPhase phase, int enabled);
void mono_debugger_startup_config (int enabled, int server, int suspend, int defer);
void mono_debugger_startup_finish (MonoStartupPhase phase, int startup, int already);
void mono_debugger_startup_transport (int transport);
void mono_debugger_startup_handshake (MonoStartupPhase phase, int fd, int result);
void mono_debugger_startup_inherited (int fd);
int mono_debugger_startup_socket (int family, int type, int protocol, MonoStartupRole role, int port);
int mono_debugger_startup_setsockopt (int fd, int level, int name, const void *value, socklen_t length, MonoStartupEvent event);
int mono_debugger_startup_bind (int fd, const struct sockaddr *address, socklen_t length);
int mono_debugger_startup_listen (int fd, int backlog);
int mono_debugger_startup_getsockname (int fd, struct sockaddr *address, socklen_t *length);
int mono_debugger_startup_poll (mono_pollfd *fds, unsigned int count, int timeout);
int mono_debugger_startup_accept (int fd);
int mono_debugger_startup_connect (int fd, const struct sockaddr *address, socklen_t length);
int mono_debugger_startup_shutdown (int fd, int how);
int mono_debugger_startup_close (int fd);
#else
#define mono_debugger_startup_init() ((void)0)
#define mono_debugger_startup_component(phase, enabled) ((void)0)
#define mono_debugger_startup_config(enabled, server, suspend, defer) ((void)0)
#define mono_debugger_startup_finish(phase, startup, already) ((void)0)
#define mono_debugger_startup_transport(transport) ((void)0)
#define mono_debugger_startup_handshake(phase, fd, result) ((void)0)
#define mono_debugger_startup_inherited(fd) ((void)0)
#define mono_debugger_startup_socket(family, type, protocol, role, port) socket (family, type, protocol)
#define mono_debugger_startup_setsockopt(fd, level, name, value, length, event) setsockopt (fd, level, name, value, length)
#define mono_debugger_startup_bind(fd, address, length) bind (fd, address, length)
#define mono_debugger_startup_listen(fd, backlog) listen (fd, backlog)
#define mono_debugger_startup_getsockname(fd, address, length) getsockname (fd, address, length)
#define mono_debugger_startup_poll(fds, count, timeout) mono_poll (fds, count, timeout)
#define mono_debugger_startup_accept(fd) accept (fd, NULL, NULL)
#define mono_debugger_startup_connect(fd, address, length) connect (fd, address, length)
#define mono_debugger_startup_shutdown(fd, how) shutdown (fd, how)
#define mono_debugger_startup_close(fd) close (fd)
#endif
#endif
