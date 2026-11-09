import { Injectable } from '@angular/core';
import { Subject, Observable, BehaviorSubject } from 'rxjs';
import { environment } from '../../../environments/environment';

export interface ChatMessage {
  id: number;
  sender_id: number;
  sender_name: string;
  content: string;
  timestamp: string;
  status: 'sent' | 'delivered' | 'read';
  image?: string;
  is_deleted?: boolean;
  reply_to?: {
    id: number;
    sender_name: string;
    content: string;
  } | null;
}

/** Why the socket is closed, so the UI can say something useful. */
export type ChatConnectionState =
  | 'connected'
  | 'connecting'
  | 'reconnecting'
  | 'signed-out'      // 4001: token missing or expired - reconnecting is pointless
  | 'not-a-member'    // 4003: not a participant of this room
  | 'offline';        // network dropped; a retry is scheduled

// Close codes the consumer uses. Retrying either of these would loop forever.
const CLOSE_UNAUTHENTICATED = 4001;
const CLOSE_FORBIDDEN = 4003;

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const RECONNECT_MAX_ATTEMPTS = 10;

@Injectable({
  providedIn: 'root'
})
export class ChatWebsocketService {
  private socket: WebSocket | null = null;
  private currentRoomId: number | null = null;

  // Reconnection bookkeeping. `onclose` used to carry the comment "Optional:
  // implement exponential backoff reconnection here" and nothing else, so a
  // single network blip, a laptop waking from sleep or a Daphne restart left
  // chat permanently dead until the user navigated away and back.
  private reconnectTimer: any = null;
  private reconnectAttempts = 0;
  private deliberateClose = false;

  // Observables for various events
  private messageSubject = new Subject<ChatMessage>();
  private typingSubject = new Subject<{user_id: number, user_name: string, is_typing: boolean}>();
  private readReceiptSubject = new Subject<{message_ids: number[], reader_id: number, room_id: number}>();
  private onlineSubject = new Subject<{user_id: number, is_online: boolean}>();
  private reactionSubject = new Subject<{message_id: number, emoji: string, action: string, user_id: number, user_name: string}>();
  private deleteSubject = new Subject<{message_id: number, deleted_by: number}>();
  private editSubject = new Subject<{message_id: number, content: string, edited_by: number}>();
  private errorSubject = new Subject<string>();

  public isConnected$ = new BehaviorSubject<boolean>(false);
  /** Finer-grained than isConnected$, so the UI can distinguish "reconnecting"
   *  from "your session expired". */
  public connectionState$ = new BehaviorSubject<ChatConnectionState>('offline');

  messages$ = this.messageSubject.asObservable();
  typing$ = this.typingSubject.asObservable();
  readReceipts$ = this.readReceiptSubject.asObservable();
  onlineStatus$ = this.onlineSubject.asObservable();
  reactions$ = this.reactionSubject.asObservable();
  deletes$ = this.deleteSubject.asObservable();
  edits$ = this.editSubject.asObservable();
  /** Server-side rejections (message too long, save failed) so a dropped
   *  message is visible instead of silent. */
  errors$ = this.errorSubject.asObservable();

  /**
   * Websocket origin, derived from the SAME `apiUrl` the HTTP calls use.
   *
   * This used to be:
   *   const host = window.location.hostname === 'localhost'
   *     ? 'localhost:8001' : window.location.host;
   *
   * which ignored `environment.apiUrl` entirely and assumed the API is served
   * from the page's own origin. Where the API has its own hostname - the
   * documented `API_URL=https://api.yourdomain.com` deployment - the socket
   * dialled the frontend host, which has no /ws/ route, so chat was dead in
   * production while every other screen worked.
   */
  private websocketOrigin(): string {
    const api = (environment.apiUrl || '').trim();

    if (api) {
      try {
        // `apiUrl` may be absolute or a same-origin prefix like '/api'.
        const resolved = new URL(api, window.location.origin);
        const protocol = resolved.protocol === 'https:' ? 'wss:' : 'ws:';
        // Keep any path prefix: a reverse proxy may mount Django under /api.
        const prefix = resolved.pathname.replace(/\/+$/, '');
        return `${protocol}//${resolved.host}${prefix}`;
      } catch {
        // Fall through to the origin below rather than throwing during connect.
      }
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}`;
  }

  connect(roomId: number) {
    if (this.socket && this.currentRoomId === roomId &&
        (this.socket.readyState === WebSocket.OPEN ||
         this.socket.readyState === WebSocket.CONNECTING)) {
      return; // Already connected, or connecting, to this room
    }

    this.disconnect();
    this.deliberateClose = false;
    this.currentRoomId = roomId;
    this.openSocket(roomId);
  }

  private openSocket(roomId: number) {
    const token = localStorage.getItem('token');
    if (!token) {
      this.connectionState$.next('signed-out');
      return;
    }

    this.connectionState$.next(
      this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

    const url = `${this.websocketOrigin()}/ws/chat/${roomId}/` +
                `?token=${encodeURIComponent(token)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      // A malformed URL threw synchronously and took the caller down with it.
      this.connectionState$.next('offline');
      this.scheduleReconnect(roomId);
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectAttempts = 0;
      this.isConnected$.next(true);
      this.connectionState$.next('connected');
    };

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.handleIncomingMessage(data);
      } catch (e) {
        console.error('Failed to parse websocket message', e);
      }
    };

    socket.onclose = (event) => {
      this.isConnected$.next(false);
      if (this.socket === socket) {
        this.socket = null;
      }

      if (this.deliberateClose) {
        this.connectionState$.next('offline');
        return;
      }

      // The consumer closes with 4001 for a missing/expired token and 4003 for
      // a room the user is not in. Retrying either would spin forever, so stop
      // and let the UI say what happened.
      if (event.code === CLOSE_UNAUTHENTICATED) {
        this.connectionState$.next('signed-out');
        this.errorSubject.next(
          'Your session has expired. Sign in again to continue chatting.');
        return;
      }
      if (event.code === CLOSE_FORBIDDEN) {
        this.connectionState$.next('not-a-member');
        this.errorSubject.next('You are no longer a member of this conversation.');
        return;
      }

      this.connectionState$.next('offline');
      this.scheduleReconnect(roomId);
    };

    socket.onerror = () => {
      // `onclose` always follows, and that is where the retry is scheduled.
      this.isConnected$.next(false);
    };
  }

  private scheduleReconnect(roomId: number) {
    if (this.reconnectTimer !== null) {
      return;
    }
    if (this.reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
      this.errorSubject.next(
        'Lost connection to chat. Reload the page to reconnect.');
      return;
    }

    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(2, this.reconnectAttempts), RECONNECT_MAX_MS);
    this.reconnectAttempts += 1;
    this.connectionState$.next('reconnecting');

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      // The user may have moved to another room while we were waiting.
      if (!this.deliberateClose && this.currentRoomId === roomId) {
        this.openSocket(roomId);
      }
    }, delay);
  }

  disconnect() {
    this.deliberateClose = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.reconnectAttempts = 0;
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
    this.currentRoomId = null;
    this.isConnected$.next(false);
    this.connectionState$.next('offline');
  }

  private handleIncomingMessage(data: any) {
    switch (data.type) {
      case 'chat_message':
        this.messageSubject.next(data as ChatMessage);
        break;
      case 'typing':
        this.typingSubject.next(data);
        break;
      case 'messages_read':
        this.readReceiptSubject.next(data);
        break;
      case 'user_online':
        this.onlineSubject.next({user_id: data.user_id, is_online: true});
        break;
      case 'user_offline':
        this.onlineSubject.next({user_id: data.user_id, is_online: false});
        break;
      case 'reaction_update':
        this.reactionSubject.next(data);
        break;
      case 'message_deleted':
        this.deleteSubject.next(data);
        break;
      case 'message_edited':
        this.editSubject.next(data);
        break;
      case 'error':
        // The consumer now reports a refused send instead of dropping it.
        this.errorSubject.next(String(data.message || 'Chat error.'));
        break;
    }
  }

  /** True when the frame went out. Callers can now tell that a send failed
   *  while the socket was down, rather than assuming it was delivered. */
  private send(payload: Record<string, unknown>): boolean {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  sendMessage(content: string, replyToId?: number): boolean {
    const sent = this.send({
      type: 'chat_message',
      content: content,
      reply_to_id: replyToId
    });
    if (!sent) {
      this.errorSubject.next(
        'Not connected. Your message was not sent - it will need to be resent '
        + 'once the connection is back.');
    }
    return sent;
  }

  sendTypingStatus(isTyping: boolean): boolean {
    return this.send({ type: isTyping ? 'typing' : 'stop_typing' });
  }

  sendReadReceipt(messageIds: number[]): boolean {
    if (!messageIds.length) {
      return false;
    }
    return this.send({ type: 'read_receipt', message_ids: messageIds });
  }

  sendReaction(messageId: number, emoji: string): boolean {
    return this.send({ type: 'reaction', message_id: messageId, emoji: emoji });
  }

  deleteMessage(messageId: number): boolean {
    return this.send({ type: 'delete_message', message_id: messageId });
  }

  editMessage(messageId: number, content: string): boolean {
    return this.send({ type: 'edit_message', message_id: messageId, content: content });
  }
}
