import { Component, OnInit, OnDestroy, ViewChild, ElementRef, ChangeDetectorRef, Inject, PLATFORM_ID, ChangeDetectionStrategy, HostListener } from '@angular/core';
import { CommonModule, isPlatformBrowser } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { SoundService } from '../../services/sound.service';
import { Subscription } from 'rxjs';
import { ChatWebsocketService, ChatMessage, ChatConnectionState } from './chat-websocket.service';

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const NEAR_BOTTOM_PX = 90;
const ROOM_REFRESH_MS = 20000;

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-chat',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './chat.component.html',
  styleUrls: ['./chat.component.css']
})
export class ChatComponent implements OnInit, OnDestroy {
  @ViewChild('chatScrollContainer') chatScrollContainer!: ElementRef;
  @ViewChild('chatFileInput') chatFileInput!: ElementRef;
  @ViewChild('composer') composer?: ElementRef<HTMLTextAreaElement>;

  isOpen = false;
  rooms: any[] = [];
  selectedRoom: any = null;
  messages: ChatMessage[] = [];
  search = '';
  isLoadingRooms = false;
  isLoadingFirst = false;

  newMessage = '';
  selectedImage: File | null = null;
  imagePreview: string | null = null;

  currentUserId: number | null = null;

  /** State of the socket for the open room, so the header can say what is going on. */
  connection: ChatConnectionState = 'offline';

  // Typing is keyed by user id (two staff can share a name) and every entry expires
  // by itself, so a missed `stop_typing` cannot leave "Bob typing..." on screen.
  typingUsers = new Map<number, string>();
  private typingExpiry = new Map<number, any>();
  onlineUsers = new Set<number>();

  /** Most recent server-side rejection, shown above the composer. */
  chatError: string | null = null;
  private chatErrorTimer: any = null;

  // Pagination
  nextUrl: string | null = null;
  isLoadingMore = false;

  // Message actions
  replyingTo: ChatMessage | null = null;
  editing: ChatMessage | null = null;
  contextMenuMsg: number | null = null;
  readonly reactionEmojis = ['👍', '❤️', '😂', '😮', '😢', '🙏'];

  // Scrolling
  atBottom = true;
  newBelow = 0;

  /** Unsent text per conversation, so switching chats does not throw a draft away. */
  private drafts = new Map<number, string>();
  private lastTypingSentAt = 0;

  get typingUsersArray(): string[] {
    return Array.from(this.typingUsers.values());
  }

  get totalUnread(): number {
    return this.rooms.reduce((sum, r) => sum + (r.unread_count || 0), 0);
  }

  /** Rooms matching the search box (name or last message). */
  get visibleRooms(): any[] {
    const q = this.search.trim().toLowerCase();
    if (!q) return this.rooms;
    return this.rooms.filter(r =>
      `${r.display_name || ''}`.toLowerCase().includes(q) ||
      `${r.last_message || ''}`.toLowerCase().includes(q));
  }

  /** The other person in a one-to-one chat. */
  get partner(): any {
    const room = this.selectedRoom;
    if (!room || room.is_group) return null;
    return (room.participants || []).find((p: any) => p.id !== this.currentUserId) || null;
  }

  /** The line under the conversation's name. */
  get statusLine(): string {
    if (this.typingUsers.size > 0) return `${this.typingUsersArray.join(', ')} typing…`;
    switch (this.connection) {
      case 'connecting': return 'Connecting…';
      case 'reconnecting': return 'Reconnecting…';
      case 'offline': return 'Offline. Trying to reconnect';
      case 'signed-out': return 'Signed out. Sign in again to chat';
      case 'not-a-member': return 'You are not in this conversation';
    }
    const room = this.selectedRoom;
    if (room?.is_group) return `${(room.participants || []).length} members`;
    if (this.partner && this.onlineUsers.has(this.partner.id)) return 'Online';
    return this.partner?.center_name && this.partner.center_name !== 'No Center' ? this.partner.center_name : 'Private chat';
  }

  get isTypingOrOnline(): boolean {
    return this.typingUsers.size > 0 || (this.connection === 'connected' && !!this.partner && this.onlineUsers.has(this.partner.id));
  }

  private setTyping(userId: number, userName: string, isTyping: boolean) {
    clearTimeout(this.typingExpiry.get(userId));
    this.typingExpiry.delete(userId);

    if (!isTyping) {
      this.typingUsers.delete(userId);
      return;
    }

    this.typingUsers.set(userId, userName || 'Someone');
    // The sender re-sends `typing` while they keep typing, so a 6 second lease
    // is comfortably longer than their keep-alive.
    this.typingExpiry.set(userId, setTimeout(() => {
      this.typingUsers.delete(userId);
      this.typingExpiry.delete(userId);
      this.cdr.markForCheck();
    }, 6000));
  }

  private showChatError(message: string) {
    this.chatError = message;
    clearTimeout(this.chatErrorTimer);
    this.chatErrorTimer = setTimeout(() => {
      this.chatError = null;
      this.cdr.markForCheck();
    }, 6000);
    this.cdr.markForCheck();
  }

  /**
   * REST rows and websocket frames describe a quoted message differently.
   * The serializer returns `reply_to` as a bare primary key and puts the preview
   * in `reply_to_preview`; the consumer sends `reply_to` as the preview object.
   * Normalise on the way in so both render the same.
   */
  private normalizeMessage(row: any): ChatMessage {
    if (row && row.reply_to_preview) {
      row.reply_to = row.reply_to_preview;
    } else if (row && (typeof row.reply_to === 'number' || row.reply_to === null)) {
      row.reply_to = null;
    }
    if (row && !row.reactions_summary) {
      row.reactions_summary = {};
    }
    // REST rows carry `deleted_at`; the template reads `is_deleted`.
    if (row && row.is_deleted === undefined) {
      row.is_deleted = !!row.deleted_at;
    }
    return row as ChatMessage;
  }

  private avatars = new Map<string, string>();

  /**
   * A deterministic initials avatar, generated locally (no request to a third party,
   * works on a till with no internet). Memoised: the template asks for it on every
   * change-detection pass.
   */
  getAvatarUrl(seed: any): string {
    const label = String(seed ?? '').trim() || 'User';
    const cached = this.avatars.get(label);
    if (cached) return cached;

    const initials = label
      .split(/\s+/)
      .slice(0, 2)
      .map(part => part.charAt(0))
      .join('')
      .toUpperCase() || 'U';

    // FNV-1a over the seed, so a given name always gets the same colour.
    let hash = 0x811c9dc5;
    for (let i = 0; i < label.length; i++) {
      hash ^= label.charCodeAt(i);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    const pairs = [['#7dd3d8', '#3fb6c0'], ['#a5b4fc', '#6366f1'], ['#fda4af', '#f43f5e'],
                   ['#fcd34d', '#f59e0b'], ['#86efac', '#22c55e'], ['#c4b5fd', '#8b5cf6'],
                   ['#93c5fd', '#3b82f6'], ['#fdba74', '#f97316']];
    const [from, to] = pairs[hash % pairs.length];

    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">` +
      `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${from}"/>` +
      `<stop offset="1" stop-color="${to}"/></linearGradient></defs>` +
      `<rect width="64" height="64" rx="32" fill="url(#g)"/>` +
      `<text x="32" y="41" text-anchor="middle" font-family="system-ui,sans-serif" ` +
      `font-size="26" font-weight="700" fill="#ffffff">${initials.replace(/[<>&"]/g, '')}</text></svg>`;

    // encodeURIComponent rather than btoa: btoa throws on non-Latin-1 names.
    const url = `data:image/svg+xml,${encodeURIComponent(svg)}`;
    this.avatars.set(label, url);
    return url;
  }

  // Subscriptions
  private subs = new Subscription();
  private typingTimeout: any;
  private roomsTimer: any = null;

  constructor(
    private api: ApiService,
    private ws: ChatWebsocketService,
    private cdr: ChangeDetectorRef,
    private sound: SoundService,
    @Inject(PLATFORM_ID) private platformId: Object
  ) {}

  ngOnInit() {
    if (isPlatformBrowser(this.platformId)) {
      const userStr = localStorage.getItem('user');
      let mayChat = false;
      if (userStr) {
        try {
          const user = JSON.parse(userStr);
          // The sign-in response stores the id as `user_id`. Reading `user.id` gave
          // undefined, so no message ever counted as the user's own: every one showed on
          // the incoming side, without ticks, and could not be edited or deleted.
          this.currentUserId = user.user_id ?? user.id ?? null;
          mayChat = user.is_superuser === true || user.permissions?.chat?.messages?.read === true;
        } catch (e) {}
      }
      // A role without Chat > Messages has no chat button; do not open the
      // room list or the socket for it (the server refuses both).
      if (!mayChat) {
        return;
      }
      this.fetchRooms();
      this.setupWebsocketListeners();
    }
  }

  ngOnDestroy() {
    this.subs.unsubscribe();
    this.clearTyping();
    this.stopRoomRefresh();
    clearTimeout(this.typingTimeout);
    clearTimeout(this.chatErrorTimer);
    this.ws.disconnect();
  }

  // ---------- small helpers for the template ----------

  trackRoom = (_: number, r: any) => r.id;
  trackMsg = (_: number, m: ChatMessage) => m.id;

  objectKeys(value: any): string[] {
    return value ? Object.keys(value) : [];
  }

  /** Tooltip listing who reacted with a given emoji. */
  reactionNames(msg: any, emoji: string): string {
    const entries = msg?.reactions_summary?.[emoji] || [];
    return entries.map((r: any) => r.user_name).filter(Boolean).join(', ');
  }

  iReacted(msg: any, emoji: string): boolean {
    return (msg?.reactions_summary?.[emoji] || []).some((r: any) => r.user_id === this.currentUserId);
  }

  isMine(msg: ChatMessage): boolean {
    return msg.sender_id === this.currentUserId;
  }

  private dayKey(ts: string): string {
    return new Date(ts).toDateString();
  }

  showDay(i: number): boolean {
    return i === 0 || this.dayKey(this.messages[i].timestamp) !== this.dayKey(this.messages[i - 1].timestamp);
  }

  /** First message of a run from the same person (shows the name in groups, the rounded corner). */
  isFirst(i: number): boolean {
    if (i === 0 || this.showDay(i)) return true;
    const a = this.messages[i - 1], b = this.messages[i];
    return a.sender_id !== b.sender_id
      || new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime() > 5 * 60 * 1000;
  }

  isLast(i: number): boolean {
    return i === this.messages.length - 1 || this.isFirst(i + 1);
  }

  dayLabel(ts: string): string {
    const d = new Date(ts);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return 'Today';
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return d.toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'short', year: 'numeric' });
  }

  /** Time for the room list: the clock today, otherwise the date. */
  roomTime(ts: string): string {
    const d = new Date(ts);
    if (d.toDateString() === new Date().toDateString()) {
      return d.toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit' });
    }
    return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  }

  canEdit(msg: ChatMessage): boolean {
    return this.isMine(msg) && !(msg as any).is_deleted && !(msg as any).image && !!msg.content;
  }

  private clearTyping() {
    this.typingExpiry.forEach(timer => clearTimeout(timer));
    this.typingExpiry.clear();
    this.typingUsers.clear();
  }

  // ---------- panel ----------

  toggleChat() {
    this.isOpen = !this.isOpen;
    if (this.isOpen) {
      this.fetchRooms();
      this.startRoomRefresh();
    } else {
      this.saveDraft();
      this.selectedRoom = null;
      this.contextMenuMsg = null;
      this.ws.disconnect();
      this.stopRoomRefresh();
    }
    this.cdr.markForCheck();
  }

  @HostListener('document:keydown.escape')
  onEscape() {
    if (!this.isOpen) return;
    if (this.contextMenuMsg !== null) { this.contextMenuMsg = null; }
    else if (this.editing) { this.cancelEdit(); }
    else if (this.replyingTo) { this.replyingTo = null; }
    else if (this.imagePreview) { this.clearImage(); }
    else { this.toggleChat(); return; }
    this.cdr.markForCheck();
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event) {
    if (this.contextMenuMsg === null) return;
    const target = event.target as HTMLElement | null;
    if (!target?.closest?.('.cx-menu, .cx-more')) {
      this.contextMenuMsg = null;
      this.cdr.markForCheck();
    }
  }

  private startRoomRefresh() {
    this.stopRoomRefresh();
    // Other conversations' last message and unread count change while this one is open.
    this.roomsTimer = setInterval(() => {
      if (this.isOpen && !document.hidden) this.fetchRooms(true);
    }, ROOM_REFRESH_MS);
  }

  private stopRoomRefresh() {
    if (this.roomsTimer) { clearInterval(this.roomsTimer); this.roomsTimer = null; }
  }

  // ---------- rooms ----------

  fetchRooms(quiet = false) {
    if (!quiet) { this.isLoadingRooms = true; }
    this.api.get('accounts/api/chat/users/').subscribe({
      next: (res) => {
        const list = Array.isArray(res) ? res : (res.results || res.data || []);
        // The room on screen is read by definition.
        this.rooms = list.map((r: any) =>
          this.selectedRoom && r.id === this.selectedRoom.id ? { ...r, unread_count: 0 } : r);
        this.isLoadingRooms = false;
        this.cdr.markForCheck();
      },
      error: (err) => {
        console.error('Failed to load rooms', err);
        this.isLoadingRooms = false;
        if (!quiet) this.showChatError('Could not load your conversations.');
        this.cdr.markForCheck();
      }
    });
  }

  selectRoom(room: any) {
    if (this.selectedRoom?.id === room.id) return;
    this.saveDraft();
    this.selectedRoom = room;
    room.unread_count = 0;
    this.messages = [];
    this.nextUrl = null;
    this.replyingTo = null;
    this.editing = null;
    this.contextMenuMsg = null;
    this.newMessage = this.drafts.get(room.id) || '';
    this.clearImage();
    this.atBottom = true;
    this.newBelow = 0;
    this.clearTyping();
    this.onlineUsers.clear();
    this.connection = 'connecting';
    this.chatError = null;

    this.fetchMessages();
    this.ws.connect(room.id);
    setTimeout(() => this.resizeComposer(), 0);
  }

  backToRooms() {
    this.saveDraft();
    this.selectedRoom = null;
    this.ws.disconnect();
    this.cdr.markForCheck();
  }

  private saveDraft() {
    if (!this.selectedRoom) return;
    if (this.newMessage.trim() && !this.editing) this.drafts.set(this.selectedRoom.id, this.newMessage);
    else this.drafts.delete(this.selectedRoom.id);
  }

  /** Keep the room list in step with what was just said. */
  private touchRoom(roomId: number, text: string, when: string) {
    const room = this.rooms.find(r => r.id === roomId);
    if (!room) return;
    room.last_message = text;
    room.last_message_time = when;
    this.rooms = [...this.rooms].sort((a, b) =>
      new Date(b.last_message_time || 0).getTime() - new Date(a.last_message_time || 0).getTime());
  }

  // ---------- messages ----------

  fetchMessages(loadMore = false) {
    if (!this.selectedRoom) return;
    const roomId = this.selectedRoom.id;

    // `next` is an absolute URL built by DRF against the API host; reduce it to the
    // path + query that ApiService expects.
    const url = loadMore && this.nextUrl
      ? this.stripApiOrigin(this.nextUrl)
      : `accounts/api/chat/messages/?room_id=${roomId}`;

    if (loadMore) this.isLoadingMore = true; else this.isLoadingFirst = true;

    this.api.get(url).subscribe({
      next: (res) => {
        // A slow answer for a conversation that has since been closed or swapped must
        // not overwrite the one on screen.
        if (!this.selectedRoom || this.selectedRoom.id !== roomId) return;

        const results = res.results || res;
        this.nextUrl = res.next || null;

        // Cursor pagination with ordering='-timestamp' gives newest first.
        const newMsgs = Array.isArray(results)
          ? [...results].reverse().map(row => this.normalizeMessage(row))
          : [];

        if (loadMore) {
          const el = this.chatScrollContainer?.nativeElement;
          const oldHeight = el ? el.scrollHeight : 0;
          this.messages = [...newMsgs, ...this.messages];
          this.isLoadingMore = false;
          this.cdr.detectChanges();
          if (el) el.scrollTop = el.scrollHeight - oldHeight; // keep the reader where they were
        } else {
          this.messages = newMsgs;
          this.isLoadingFirst = false;
          this.scrollToBottom(true);
          this.markRoomAsRead();
        }
        this.isLoadingMore = false;
        this.isLoadingFirst = false;
        this.cdr.markForCheck();
      },
      error: (err) => {
        console.error('Failed to fetch messages', err);
        this.isLoadingMore = false;
        this.isLoadingFirst = false;
        this.showChatError('Could not load the messages. Close and reopen the chat to try again.');
      }
    });
  }

  /** Reduce an absolute API URL to the path+query `ApiService` expects. */
  private stripApiOrigin(url: string): string {
    try {
      const parsed = new URL(url, window.location.origin);
      const base = new URL(this.api.baseUrl || '/', window.location.origin);
      const prefix = base.pathname.replace(/\/+$/, '');
      let path = parsed.pathname;
      if (prefix && path.startsWith(prefix)) {
        path = path.slice(prefix.length);
      }
      return path.replace(/^\/+/, '') + parsed.search;
    } catch {
      return url.replace(/^https?:\/\/[^/]+\//, '');
    }
  }

  onScroll(event: any) {
    const el = event.target as HTMLElement;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    if (atBottom !== this.atBottom || (atBottom && this.newBelow)) {
      this.atBottom = atBottom;
      if (atBottom) this.newBelow = 0;
      this.cdr.markForCheck();
    }
    if (el.scrollTop === 0 && this.nextUrl && !this.isLoadingMore) {
      this.fetchMessages(true);
    }
  }

  scrollToBottom(force = false) {
    setTimeout(() => {
      const el = this.chatScrollContainer?.nativeElement;
      if (!el) return;
      if (force || this.atBottom) {
        if (typeof el.scrollTo === 'function') el.scrollTo({ top: el.scrollHeight, behavior: force ? 'auto' : 'smooth' });
        else el.scrollTop = el.scrollHeight;
        this.atBottom = true;
        this.newBelow = 0;
        this.cdr.markForCheck();
      }
    }, 30);
  }

  jumpToLatest() {
    this.scrollToBottom(true);
  }

  // ---------- websocket ----------

  private setupWebsocketListeners() {
    this.subs.add(
      this.ws.connectionState$.subscribe(state => {
        this.connection = state;
        this.cdr.markForCheck();
      })
    );

    this.subs.add(
      this.ws.messages$.subscribe(msg => {
        if (this.messages.some(m => m.id === msg.id)) {
          // A message sent over REST is also broadcast, so the sender can see an id twice.
          return;
        }
        const mine = msg.sender_id === this.currentUserId;
        const wasAtBottom = this.atBottom;
        this.messages = [...this.messages, this.normalizeMessage(msg)];
        if (this.selectedRoom) {
          this.touchRoom(this.selectedRoom.id, msg.content || (msg.image ? '[Image]' : ''), msg.timestamp);
        }

        if (mine || wasAtBottom) {
          this.scrollToBottom(true);
        } else {
          this.newBelow++;
        }

        if (!mine) {
          this.sound.play('receive');
          // Read receipt only while the panel is open, otherwise a background socket
          // would clear the unread badge for messages nobody has looked at.
          if (this.isOpen) {
            this.ws.sendReadReceipt([msg.id]);
            this.setTyping(msg.sender_id, '', false);
          }
        }
        this.cdr.markForCheck();
      })
    );

    this.subs.add(
      this.ws.typing$.subscribe(data => {
        if (data.user_id === this.currentUserId) return;
        this.setTyping(data.user_id, data.user_name, data.is_typing);
        this.cdr.markForCheck();
      })
    );

    this.subs.add(
      this.ws.readReceipts$.subscribe(data => {
        // `messages_read` goes to the sender's own user group, so it can arrive for a
        // room other than the one on screen.
        if (data.room_id && this.selectedRoom && data.room_id !== this.selectedRoom.id) {
          return;
        }
        this.messages.forEach(m => {
          if (m.sender_id === this.currentUserId && data.message_ids.includes(m.id)) {
            m.status = 'read';
          }
        });
        this.cdr.markForCheck();
      })
    );

    this.subs.add(
      this.ws.edits$.subscribe(data => {
        const msg = this.messages.find(m => m.id === data.message_id);
        if (msg) {
          msg.content = data.content;
          (msg as any).edited_at = new Date().toISOString();
          this.cdr.markForCheck();
        }
      })
    );

    this.subs.add(
      this.ws.errors$.subscribe(message => this.showChatError(message))
    );

    this.subs.add(
      this.ws.onlineStatus$.subscribe(data => {
        if (data.is_online) this.onlineUsers.add(data.user_id);
        else this.onlineUsers.delete(data.user_id);
        this.cdr.markForCheck();
      })
    );

    this.subs.add(
      this.ws.reactions$.subscribe(data => {
        const msg: any = this.messages.find(m => m.id === data.message_id);
        if (!msg) {
          return;
        }
        const summary = { ...(msg.reactions_summary || {}) };

        // One reaction per user per message: drop their previous emoji first.
        Object.keys(summary).forEach(emoji => {
          summary[emoji] = (summary[emoji] || []).filter((r: any) => r.user_id !== data.user_id);
          if (!summary[emoji].length) {
            delete summary[emoji];
          }
        });

        if (data.action === 'added' || data.action === 'changed') {
          summary[data.emoji] = [
            ...(summary[data.emoji] || []),
            { user_id: data.user_id, user_name: data.user_name },
          ];
        }
        msg.reactions_summary = summary;
        this.cdr.markForCheck();
      })
    );

    this.subs.add(
      this.ws.deletes$.subscribe(data => {
        const msg = this.messages.find(m => m.id === data.message_id);
        if (msg) {
          msg.content = '';
          msg.is_deleted = true;
          this.cdr.markForCheck();
        }
      })
    );
  }

  // ---------- composing ----------

  onTyping() {
    this.resizeComposer();
    const now = Date.now();
    // `typing` once every 1.5 s while typing, not on every keystroke.
    if (now - this.lastTypingSentAt > 1500) {
      this.lastTypingSentAt = now;
      this.ws.sendTypingStatus(true);
    }
    clearTimeout(this.typingTimeout);
    this.typingTimeout = setTimeout(() => {
      this.lastTypingSentAt = 0;
      this.ws.sendTypingStatus(false);
    }, 2000);
  }

  onComposerKeydown(event: KeyboardEvent) {
    // Enter sends, Shift+Enter is a new line. Not while an input method is composing.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      this.sendMessage();
    }
  }

  private resizeComposer() {
    const el = this.composer?.nativeElement;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  }

  get canSend(): boolean {
    return !!this.newMessage.trim() || !!this.selectedImage;
  }

  sendMessage() {
    if (!this.selectedRoom || !this.canSend) return;
    const text = this.newMessage.trim();

    if (this.editing) {
      if (!text) return;
      if (text === this.editing.content) { this.cancelEdit(); return; }
      if (this.ws.editMessage(this.editing.id, text)) {
        this.sound.play('send');
        this.cancelEdit();
      } else {
        this.showChatError('Not connected. The change was not saved.');
      }
      return;
    }

    if (this.selectedImage) {
      // Pictures go over REST (the socket carries text only).
      const fd = new FormData();
      fd.append('room_id', this.selectedRoom.id);
      if (text) fd.append('content', text);
      if (this.replyingTo) fd.append('reply_to', this.replyingTo.id.toString());
      fd.append('image', this.selectedImage);

      this.api.post('accounts/api/chat/messages/', fd).subscribe({
        next: () => {
          this.sound.play('send');
          this.resetInput();
        },
        error: (err) => {
          console.error('Failed to send image', err);
          this.showChatError(err?.error?.detail || 'The image could not be sent. Try again.');
        }
      });
    } else if (this.ws.sendMessage(text, this.replyingTo?.id)) {
      // `sendMessage` reports whether the frame went out, so a message typed while the
      // socket is down stays in the box instead of vanishing.
      this.sound.play('send');
      this.resetInput();
    }
  }

  resetInput() {
    this.newMessage = '';
    this.clearImage();
    this.replyingTo = null;
    this.editing = null;
    if (this.selectedRoom) this.drafts.delete(this.selectedRoom.id);
    clearTimeout(this.typingTimeout);
    this.lastTypingSentAt = 0;
    this.ws.sendTypingStatus(false);
    setTimeout(() => this.resizeComposer(), 0);
    this.cdr.markForCheck();
  }

  onFileSelected(event: any) {
    const file: File | undefined = event.target.files?.[0];
    // Reset so choosing the same file again still fires `change`.
    if (event.target) event.target.value = '';
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      this.showChatError('Only pictures can be attached.');
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      this.showChatError('That picture is larger than 10 MB.');
      return;
    }
    this.selectedImage = file;
    const reader = new FileReader();
    reader.onload = (e: any) => { this.imagePreview = e.target.result; this.cdr.markForCheck(); };
    reader.readAsDataURL(file);
  }

  clearImage() {
    this.selectedImage = null;
    this.imagePreview = null;
  }

  markRoomAsRead() {
    if (!this.selectedRoom) return;
    this.api.post('accounts/api/chat/messages/mark_read/', { room_id: this.selectedRoom.id }).subscribe({
      error: () => undefined, // the badge is refreshed with the next room list anyway
    });
  }

  // ---------- message actions ----------

  openMenu(msgId: number, event: Event) {
    event.preventDefault();
    event.stopPropagation();
    this.contextMenuMsg = this.contextMenuMsg === msgId ? null : msgId;
    if (this.contextMenuMsg !== null) {
      // The menu opens below the bubble: bring it fully into view if it is near the bottom.
      const bubble = (event.target as HTMLElement | null)?.closest?.('.cx-bubble');
      setTimeout(() => (bubble?.querySelector('.cx-menu') as HTMLElement | null)?.scrollIntoView?.({ block: 'nearest' }), 30);
    }
  }

  replyToMessage(msg: ChatMessage) {
    this.replyingTo = msg;
    this.editing = null;
    this.contextMenuMsg = null;
    this.focusComposer();
  }

  startEdit(msg: ChatMessage) {
    this.editing = msg;
    this.replyingTo = null;
    this.contextMenuMsg = null;
    this.newMessage = msg.content;
    this.focusComposer();
  }

  cancelEdit() {
    this.editing = null;
    this.newMessage = '';
    setTimeout(() => this.resizeComposer(), 0);
  }

  react(msg: ChatMessage, emoji: string) {
    this.contextMenuMsg = null;
    if (!this.ws.sendReaction(msg.id, emoji)) {
      this.showChatError('Not connected. The reaction was not sent.');
    }
  }

  copyMessage(msg: ChatMessage) {
    this.contextMenuMsg = null;
    navigator.clipboard?.writeText(msg.content || '').catch(() => undefined);
  }

  deleteMessage(msgId: number) {
    this.contextMenuMsg = null;
    if (!this.ws.deleteMessage(msgId)) {
      this.showChatError('Not connected. The message was not deleted.');
    }
  }

  openImage(url: string) {
    window.open(url, '_blank', 'noopener');
  }

  private focusComposer() {
    setTimeout(() => {
      const el = this.composer?.nativeElement;
      if (el) { el.focus(); this.resizeComposer(); }
    }, 0);
  }
}
