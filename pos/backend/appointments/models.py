from django.db import models
from salon_admin.models import Center
from staff.models import StaffMember

class Appointment(models.Model):
    STATUS_CHOICES = (
        ('Scheduled', 'Scheduled'),
        ('Checked-in', 'Checked-in'),       # client has arrived (D14)
        ('Completed', 'Completed'),         # set by billing only
        ('No-show', 'No-show'),             # client never came (D14)
        ('Cancelled', 'Cancelled'),
    )
    # Statuses that hold a stylist's time slot.
    BOOKED = ('Scheduled', 'Checked-in')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='appointments')
    client = models.ForeignKey(
        'clients.Client',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='appointments'
    )
    client_phone = models.CharField(max_length=20)
    client_name = models.CharField(max_length=100)
    date = models.DateField(db_index=True)
    start_time = models.TimeField()
    notes = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Scheduled')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.client_name} - {self.date} {self.start_time}"

    class Meta:
        indexes = [
            models.Index(fields=['center', 'date'], name='appt_center_date_idx'),
            models.Index(fields=['status'], name='appt_status_idx'),
            models.Index(fields=['created_at'], name='appt_created_idx'),
            models.Index(fields=['center', 'status'], name='appt_center_status_idx'),
            # The day sheet: one branch, one date, ordered by time. Without the
            # sort column in the index the database gathers the whole day and
            # sorts it in memory - fine for twelve appointments, not for a
            # multi-branch day across a year of history.
            models.Index(fields=['center', 'date', 'status'],
                         name='appt_center_date_status_idx'),
            # "Has this customer been in before?" - looked up by phone at the
            # counter, and by the till when attaching a walk-in.
            models.Index(fields=['client_phone'], name='appt_client_phone_idx'),
        ]

class AppointmentService(models.Model):
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='services')
    # The catalogue service (B5.5). Old lines were free text; the name is kept
    # as written so history still reads the same if the catalogue changes.
    service = models.ForeignKey('services.ServiceMaster', on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='appointment_lines')
    service_name = models.CharField(max_length=100)
    time = models.TimeField()
    duration = models.IntegerField(help_text="Duration in minutes")
    staff = models.ForeignKey(StaffMember, on_delete=models.SET_NULL, null=True, blank=True, related_name='appointments')
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    class Meta:
        indexes = [
            models.Index(fields=['staff', 'appointment']),
        ]

    @property
    def end_time(self):
        import datetime
        if not self.time:
            return None
        start = datetime.datetime.combine(datetime.date.min, self.time)
        end = start + datetime.timedelta(minutes=int(self.duration or 0))
        return end.time() if end.date() == datetime.date.min else datetime.time(23, 59)

    def __str__(self):
        return f"{self.service_name} for {self.appointment.client_name}"
