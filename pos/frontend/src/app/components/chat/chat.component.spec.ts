import { TestBed } from '@angular/core/testing';
import { BehaviorSubject, Subject, of } from 'rxjs';

import { ApiService } from '../../services/api';
import { ChatComponent } from './chat.component';
import { ChatWebsocketService } from './chat-websocket.service';

/** A stand-in for the socket: tests push frames through the subjects. */
class FakeWs {
  messages$ = new Subject<any>();
  typing$ = new Subject<any>();
  readReceipts$ = new Subject<any>();
  onlineStatus$ = new Subject<any>();
  reactions$ = new Subject<any>();
  deletes$ = new Subject<any>();
  edits$ = new Subject<any>();
  errors$ = new Subject<string>();
  connectionState$ = new BehaviorSubject<string>('offline');
  connected = true;
  sent: any[] = [];
  connect = (id: number) => { this.sent.push(['connect', id]); };
  disconnect = () => undefined;
  sendMessage = (text: string, replyTo?: number) => { if (this.connected) this.sent.push(['msg', text, replyTo]); return this.connected; };
  sendTypingStatus = () => true;
  sendReadReceipt = () => true;
  sendReaction = (id: number, emoji: string) => { this.sent.push(['react', id, emoji]); return this.connected; };
  deleteMessage = () => true;
  editMessage = (id: number, text: string) => { this.sent.push(['edit', id, text]); return this.connected; };
}

const ROOMS = [
  { id: 1, display_name: 'Abha Bhatnagar', last_message: 'see you at 5', is_group: false, participants: [{ id: 7 }, { id: 9, center_name: 'Delhi' }], unread_count: 2 },
  { id: 2, display_name: 'Front desk', last_message: null, is_group: true, participants: [{ id: 7 }, { id: 9 }, { id: 10 }], unread_count: 0 },
];

function setup(getImpl?: (url: string) => any) {
  // as the real sign-in response stores it: `user_id`, not `id`
  localStorage.setItem('user', JSON.stringify({ user_id: 7, is_superuser: true }));
  const ws = new FakeWs();
  const api = {
    baseUrl: 'http://localhost:8001',
    get: jasmine_or_vi((url: string) => (getImpl ? getImpl(url) : url.includes('/users/') ? of(ROOMS) : of({ results: [], next: null }))),
    post: () => of({}),
  };
  TestBed.configureTestingModule({
    providers: [{ provide: ApiService, useValue: api }, { provide: ChatWebsocketService, useValue: ws }],
  });
  const fixture = TestBed.createComponent(ChatComponent);
  fixture.detectChanges();
  return { fixture, comp: fixture.componentInstance, ws, api };
}

// The suite runs under vitest: a plain function is enough as a stub.
function jasmine_or_vi<T extends (...a: any[]) => any>(fn: T): T { return fn; }

describe('ChatComponent', () => {
  afterEach(() => localStorage.removeItem('user'));

  it('loads the conversations and filters them from the search box', () => {
    const { comp } = setup();
    expect(comp.rooms.length).toBe(2);
    comp.search = 'abha';
    expect(comp.visibleRooms.map(r => r.id)).toEqual([1]);
    comp.search = 'nothing like this';
    expect(comp.visibleRooms).toEqual([]);
    comp.search = '';
    expect(comp.visibleRooms.length).toBe(2);
  });

  it('knows which messages are the signed-in users own', () => {
    const { comp } = setup();
    expect(comp.currentUserId).toBe(7);
    expect(comp.isMine({ sender_id: 7 } as any)).toBe(true);
    expect(comp.isMine({ sender_id: 9 } as any)).toBe(false);
  });

  it('adds up unread messages across conversations', () => {
    const { comp } = setup();
    expect(comp.totalUnread).toBe(2);
  });

  it('opening a conversation clears its unread badge and connects to it', () => {
    const { comp, ws } = setup();
    comp.selectRoom(comp.rooms[0]);
    expect(comp.rooms[0].unread_count).toBe(0);
    expect(ws.sent).toContainEqual(['connect', 1]);
  });

  it('ignores a slow answer for a conversation that was closed meanwhile', () => {
    const slow = new Subject<any>();
    const { comp } = setup(url => (url.includes('/users/') ? of(ROOMS) : url.includes('room_id=1') ? slow : of({ results: [], next: null })));
    comp.selectRoom(comp.rooms[0]);   // its messages are still on the way...
    comp.selectRoom(comp.rooms[1]);   // ...when another conversation is opened
    slow.next({ results: [{ id: 99, sender_id: 9, content: 'wrong room', timestamp: '2026-10-19T10:00:00' }], next: null });
    expect(comp.messages).toEqual([]);
  });

  it('groups a run of messages from one person and starts a new day', () => {
    const { comp } = setup();
    comp.messages = [
      { id: 1, sender_id: 9, content: 'a', timestamp: '2026-10-18T09:00:00', status: 'read' },
      { id: 2, sender_id: 9, content: 'b', timestamp: '2026-10-18T09:01:00', status: 'read' },
      { id: 3, sender_id: 7, content: 'c', timestamp: '2026-10-18T09:02:00', status: 'sent' },
      { id: 4, sender_id: 7, content: 'd', timestamp: '2026-10-19T09:02:00', status: 'sent' },
    ] as any;
    expect(comp.showDay(0)).toBe(true);
    expect(comp.showDay(1)).toBe(false);
    expect(comp.showDay(3)).toBe(true);
    expect(comp.isFirst(1)).toBe(false);   // same person a minute later
    expect(comp.isFirst(2)).toBe(true);    // someone else
    expect(comp.isLast(1)).toBe(true);
    expect(comp.isFirst(3)).toBe(true);    // a new day starts a new run
  });

  it('keeps the text in the box when the socket is down', () => {
    const { comp, ws } = setup();
    comp.selectRoom(comp.rooms[0]);
    ws.connected = false;
    comp.newMessage = 'hello';
    comp.sendMessage();
    expect(comp.newMessage).toBe('hello');
  });

  it('sends on Enter but not on Shift+Enter', () => {
    const { comp, ws } = setup();
    comp.selectRoom(comp.rooms[0]);
    comp.newMessage = 'hi there';
    comp.onComposerKeydown(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true }));
    expect(ws.sent.filter(s => s[0] === 'msg').length).toBe(0);
    comp.onComposerKeydown(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(ws.sent).toContainEqual(['msg', 'hi there', undefined]);
    expect(comp.newMessage).toBe('');
  });

  it('edits your own message and refuses to edit someone else\'s', () => {
    const { comp, ws } = setup();
    comp.selectRoom(comp.rooms[0]);
    const mine: any = { id: 5, sender_id: 7, content: 'typo', timestamp: '2026-10-19T09:00:00', status: 'sent' };
    const theirs: any = { id: 6, sender_id: 9, content: 'x', timestamp: '2026-10-19T09:00:00', status: 'sent' };
    expect(comp.canEdit(mine)).toBe(true);
    expect(comp.canEdit(theirs)).toBe(false);
    comp.startEdit(mine);
    comp.newMessage = 'fixed';
    comp.sendMessage();
    expect(ws.sent).toContainEqual(['edit', 5, 'fixed']);
    expect(comp.editing).toBeNull();
  });

  it('keeps a draft per conversation', () => {
    const { comp } = setup();
    comp.selectRoom(comp.rooms[0]);
    comp.newMessage = 'half written';
    comp.selectRoom(comp.rooms[1]);
    expect(comp.newMessage).toBe('');
    comp.selectRoom(comp.rooms[0]);
    expect(comp.newMessage).toBe('half written');
  });

  it('gives the same avatar for the same name without making a new one each time', () => {
    const { comp } = setup();
    expect(comp.getAvatarUrl('Abha Bhatnagar')).toBe(comp.getAvatarUrl('Abha Bhatnagar'));
    expect(comp.getAvatarUrl('Abha Bhatnagar')).toContain('AB');
  });
});
