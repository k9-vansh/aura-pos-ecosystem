import { ComponentFixture, TestBed } from '@angular/core/testing';

import { HeldCartsModalComponent } from './held-carts-modal';

describe('HeldCartsModalComponent', () => {
  let component: HeldCartsModalComponent;
  let fixture: ComponentFixture<HeldCartsModalComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [HeldCartsModalComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(HeldCartsModalComponent);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
