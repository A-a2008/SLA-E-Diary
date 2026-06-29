import secrets

from django.db import models
from django.contrib.auth.models import User, Group


class UserRole(models.TextChoices):
    ADMIN = 'admin', 'Admin'
    ADVOCATE = 'advocate', 'Advocate'
    INTERN = 'intern', 'Intern'

    @classmethod
    def group_name(cls, role):
        return f'{role}_users'


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.INTERN)
    phone = models.CharField(max_length=20, blank=True, null=True)
    left_on = models.DateField(blank=True, null=True)
    telegram_code = models.CharField(max_length=6, unique=True, null=True, blank=True)
    telegram_chat_id = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.role})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if not self.telegram_code:
            self.telegram_code = self._generate_code()
        super().save(*args, **kwargs)
        self._sync_group(is_new)

    @staticmethod
    def _generate_code():
        while True:
            code = ''.join(secrets.choice('0123456789') for _ in range(6))
            if not UserProfile.objects.filter(telegram_code=code).exists():
                return code

    def _sync_group(self, is_new=False):
        group_name = UserRole.group_name(self.role)
        group, _ = Group.objects.get_or_create(name=group_name)
        for g in self.user.groups.all():
            if g.name.endswith('_users') and g.name != group_name:
                self.user.groups.remove(g)
        self.user.groups.add(group)


class Jurisdiction(models.TextChoices):
    URBAN = 'urban', 'Bengaluru Urban'
    RURAL = 'rural', 'Bengaluru Rural'


class Party1Type(models.TextChoices):
    PETITIONER = 'Petitioner', 'Petitioner'
    PLAINTIFF = 'Plaintiff', 'Plaintiff'
    APPLICANT = 'Applicant', 'Applicant'
    COMPLAINANT = 'Complainant', 'Complainant'
    DECREE_HOLDER = 'Decree Holder', 'Decree Holder'
    CAVEATOR = 'Caveator', 'Caveator'


class Party2Type(models.TextChoices):
    DEFENDANT = 'Defendant', 'Defendant'
    RESPONDENT = 'Respondent(s)', 'Respondent(s)'
    JUDGMENT_DEBTOR = 'Judgment Debtor', 'Judgment Debtor'
    ACCUSED = 'Accused', 'Accused'
    OPPOSITE_PARTY = 'Opposite Party/ies', 'Opposite Party/ies'


class CourtLevel(models.TextChoices):
    DISTRICT = 'district', 'District Court'
    HIGH_COURT = 'high_court', 'High Court of Karnataka'
    SUPREME_COURT = 'supreme_court', 'Supreme Court of India'


class MediationStatus(models.TextChoices):
    NONE = 'none', 'Not in Mediation'
    REFERRED = 'referred', 'Referred to Mediation'
    ONGOING = 'ongoing', 'Mediation Ongoing'
    SETTLED = 'settled', 'Settled at Mediation'
    FAILED = 'failed', 'Mediation Failed'


class MediationEntryType(models.TextChoices):
    BUSINESS = 'business', 'Court Business'
    MEDIATION = 'mediation', 'Mediation'


class Case(models.Model):
    jurisdiction = models.CharField(max_length=20, choices=Jurisdiction.choices, default=None)
    court_level = models.CharField(max_length=20, choices=CourtLevel.choices)
    mediation_status = models.CharField(max_length=20, choices=MediationStatus.choices, default=MediationStatus.NONE)
    mediation_next_date = models.DateField(null=True, blank=True)
    court = models.CharField(max_length=100)
    court_hall = models.CharField(max_length=100)
    floor = models.IntegerField()
    case_type = models.CharField(max_length=100)
    case_number = models.CharField(max_length=100)
    case_year = models.IntegerField()
    party_1 = models.CharField(max_length=100)
    party_1_type = models.CharField(max_length=20, choices=Party1Type.choices)
    party_2 = models.CharField(max_length=100)
    party_2_type = models.CharField(max_length=20, choices=Party2Type.choices)
    representing = models.CharField(max_length=100)
    representing_parties = models.CharField(max_length=100, default='1')
    party_1_total = models.IntegerField(default=1)
    party_2_total = models.IntegerField(default=1)
    disposed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.case_type} {self.case_number}/{self.case_year} — {self.party_1} vs {self.party_2}"

    def case_number_display(self):
        return f"{self.case_type}/{self.case_number}/{self.case_year}"

    @property
    def represents_party_1(self):
        return self.representing == self.party_1_type

    @property
    def represents_party_2(self):
        return self.representing == self.party_2_type

    @property
    def representing_display(self):
        if self.representing_parties != '1':
            return f"{self.representing} {self.representing_parties}"
        return self.representing


class DiaryEntry(models.Model):
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='diary_entries')
    entry_type = models.CharField(max_length=20, choices=MediationEntryType.choices, default=MediationEntryType.BUSINESS)
    previous_date = models.DateField()
    court = models.CharField(max_length=100)
    court_hall = models.CharField(max_length=100)
    floor = models.IntegerField()
    case_number_display = models.CharField(max_length=200)
    representing = models.CharField(max_length=100)
    representing_parties = models.CharField(max_length=100, default='1')
    party_1_total = models.IntegerField(default=1)
    party_2_total = models.IntegerField(default=1)
    stage = models.CharField(max_length=100)
    business = models.TextField()
    next_date = models.DateField()
    advocate = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def represents_party_1(self):
        return self.representing == self.case.party_1_type

    @property
    def represents_party_2(self):
        return self.representing == self.case.party_2_type

    def __str__(self):
        return f"Diary Entry for {self.case} on {self.previous_date}"

    class Meta:
        ordering = ['-previous_date']


class CauseListEntry(models.Model):
    date = models.DateField(db_index=True)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='cause_list_entries')
    list_i = models.IntegerField(blank=True, null=True)
    list_ii = models.IntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CL {self.date} – {self.case} (I:{self.list_i or '-'} II:{self.list_ii or '-'})"

    class Meta:
        ordering = ['date']
        unique_together = ['date', 'case']


class CourtHallNote(models.Model):
    court = models.CharField(max_length=100)
    court_hall = models.CharField(max_length=100)
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['court', 'court_hall']
        verbose_name = 'Court Hall Note'
        verbose_name_plural = 'Court Hall Notes'

    def __str__(self):
        return f"Note for {self.court_hall}"


class ReminderFrequency(models.TextChoices):
    DAILY = 'daily', 'Every Day'
    ALTERNATE = 'alternate', 'Alternate Days'
    TWICE_WEEK = 'twice_week', 'Twice a Week'
    WEEKLY = 'weekly', 'Once a Week'


class Reminder(models.Model):
    diary_entry = models.ForeignKey(DiaryEntry, on_delete=models.CASCADE, related_name='reminders')
    task = models.CharField(max_length=300)
    start_on = models.DateField()
    frequency = models.CharField(max_length=20, choices=ReminderFrequency.choices, default=ReminderFrequency.DAILY)
    ramp_up = models.BooleanField(default=False,
                                   help_text='Increase frequency as next hearing date approaches')
    completed = models.BooleanField(default=False)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Reminder: {self.task[:60]} ({'✔' if self.completed else '⏳'})"


class OutgoingMessage(models.Model):
    chat_id = models.CharField(max_length=100)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Msg to {self.chat_id}: {self.text[:60]} ({'sent' if self.sent else 'pending'})"
