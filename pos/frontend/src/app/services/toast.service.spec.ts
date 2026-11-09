import { TestBed } from '@angular/core/testing';
import { ToastService } from './toast.service';

describe('ToastService.show', () => {
  it('shows one refusal once even when two handlers report it', () => {
    const toast = TestBed.inject(ToastService);
    toast.showError('Password: This password is too common.');
    toast.showError('password: This password is too common.');
    expect(toast.toasts().length).toBe(1);
  });

  it('still shows different errors, and repeats after the first is gone', () => {
    const toast = TestBed.inject(ToastService);
    toast.showError('Email: already exists.');
    toast.showError('Phone: must be 10 digits.');
    expect(toast.toasts().length).toBe(2);
    toast.remove(toast.toasts()[0].id);
    toast.showError('Email: already exists.');
    expect(toast.toasts().length).toBe(2);
  });

  it('never merges short or empty messages', () => {
    const toast = TestBed.inject(ToastService);
    toast.showError('Failed:');
    toast.showError('Failed to save the thing.');
    expect(toast.toasts().length).toBe(2);
  });
});

describe('ToastService.plain', () => {
  it('turns a server refusal into words', () => {
    expect(ToastService.plain('Failed to create product: {"gst_percent":["Must be 0 to 100."]}'))
      .toBe('Failed to create product: Gst percent: Must be 0 to 100.');
  });

  it('joins several fields and lists', () => {
    expect(ToastService.plain('Failed: {"name":["Blank."],"expiry_days":["Too long.","Not a number."]}'))
      .toBe('Failed: Name: Blank. | Expiry days: Too long. | Expiry days: Not a number.');
  });

  it('reads {"error": "..."} and {"detail": "..."} without a label', () => {
    expect(ToastService.plain('Failed to receive stock: {"error":"This batch has already expired."}'))
      .toBe('Failed to receive stock: This batch has already expired.');
    expect(ToastService.plain('Failed: {"detail":"Not allowed."}')).toBe('Failed: Not allowed.');
  });

  it('reads a nested list of lines', () => {
    expect(ToastService.plain('Failed: {"items":{"0":{"quantity":["Must be more than zero."]}}}'))
      .toBe('Failed: Quantity: Must be more than zero.');
  });

  it('leaves ordinary text and unreadable braces alone', () => {
    expect(ToastService.plain('Stock received.')).toBe('Stock received.');
    expect(ToastService.plain('Pick [one] of {these}')).toBe('Pick [one] of {these}');
    expect(ToastService.plain('')).toBe('');
  });
});
