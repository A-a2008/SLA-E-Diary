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

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.role})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        self._sync_group(is_new)

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


class CourtLevel(models.TextChoices):
    DISTRICT = 'district', 'District Court'
    HIGH_COURT = 'high_court', 'High Court of Karnataka'
    SUPREME_COURT = 'supreme_court', 'Supreme Court of India'


class Case(models.Model):
    jurisdiction = models.CharField(max_length=20, choices=Jurisdiction.choices, default=None)
    court_level = models.CharField(max_length=20, choices=CourtLevel.choices)
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
    disposed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.case_type} {self.case_number}/{self.case_year} — {self.party_1} vs {self.party_2}"

    def case_number_display(self):
        return f"{self.case_type}/{self.case_number}/{self.case_year}"

    @property
    def representing_display(self):
        return self.representing


class DiaryEntry(models.Model):
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='diary_entries')
    previous_date = models.DateField()
    court = models.CharField(max_length=100)
    court_hall = models.CharField(max_length=100)
    floor = models.IntegerField()
    case_number_display = models.CharField(max_length=200)
    representing = models.CharField(max_length=100)
    stage = models.CharField(max_length=100)
    business = models.TextField()
    list_i = models.IntegerField(blank=True, null=True)
    list_ii = models.IntegerField(blank=True, null=True)
    next_date = models.DateField()
    advocate = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Diary Entry for {self.case} on {self.previous_date}"

    class Meta:
        ordering = ['-previous_date']
