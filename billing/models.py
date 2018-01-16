import calendar
import uuid
from datetime import date, datetime
from typing import List, Tuple

import re
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Model, PROTECT, CASCADE, QuerySet, Sum
from django.utils.translation import ugettext_lazy as _
from django_fsm import FSMField, can_proceed, transition
from djmoney.models.fields import CurrencyField, MoneyField
from moneyed import Money

from .total import Total


def total_amount(qs) -> Total:
    """Sums the amounts of the objects in the queryset, keeping each currency separate.
    :param qs: A querystring containing objects that have an amount field of type Money.
    :return: A Total object.
    """
    aggregate = qs.values('amount_currency').annotate(sum=Sum('amount'))
    return Total(Money(amount=r['sum'], currency=r['amount_currency']) for r in aggregate)


########################################################################################################


class OnlyOpenAccountsManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(status=Account.OPEN)

    def with_uninvoiced_charges(self):
        return self.filter(charges__isnull=False, charges__invoice__isnull=True)


class Account(Model):
    OPEN = 'OPEN'
    CLOSED = 'CLOSED'
    STATUS_CHOICES = (
        (OPEN, _('Open')),
        (CLOSED, _('Closed')),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, related_name='billing_account', on_delete=PROTECT)
    currency = CurrencyField(db_index=True)
    status = FSMField(max_length=20, choices=STATUS_CHOICES, default=OPEN, db_index=True)

    objects = models.Manager()
    open = OnlyOpenAccountsManager()

    def balance(self, as_of: date = None):
        charges = Charge.objects.filter(account=self)
        transactions = Transaction.successful.filter(account=self)
        if as_of is not None:
            charges = charges.filter(created__lte=as_of)
            transactions = transactions.filter(created__lte=as_of)
        return total_amount(transactions) - total_amount(charges)

    @transition(field=status, source=OPEN, target=CLOSED)
    def close(self):
        pass

    @transition(field=status, source=CLOSED, target=OPEN)
    def reopen(self):
        pass

    def has_past_due_invoices(self):
        return Invoice.objects.filter(account=self, status=Invoice.PAST_DUE).exists()

    def __str__(self):
        return str(self.owner)


########################################################################################################

class InvoiceManager(models.Manager):
    def payable(self) -> QuerySet:
        return Invoice.objects.filter(status__in=[Invoice.PENDING, Invoice.PAST_DUE])


class Invoice(Model):
    PENDING = 'PENDING'
    PAST_DUE = 'PAST_DUE'
    PAYED = 'PAYED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = (
        (PENDING, _('Pending')),
        (PAST_DUE, _('Past-due')),
        (PAYED, _('Payed')),
        (CANCELLED, _('Cancelled')),
    )
    account = models.ForeignKey(Account, related_name='invoices', on_delete=PROTECT)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    status = FSMField(max_length=20, choices=STATUS_CHOICES, default=PENDING, db_index=True)

    objects = InvoiceManager()

    @transition(field=status, source=PENDING, target=PAST_DUE)
    def mark_past_due(self):
        pass

    @transition(field=status, source=[PENDING, PAST_DUE], target=PAYED)
    def pay(self):
        pass

    @transition(field=status, source=[PENDING, PAST_DUE], target=CANCELLED)
    def cancel(self):
        pass

    @property
    def in_payable_state(self):
        return can_proceed(self.pay)

    def total(self):
        return total_amount(Charge.objects.filter(invoice=self))

    def __str__(self):
        return '#{}'.format(self.id)


########################################################################################################

product_code_validator = RegexValidator(regex=r'^[A-Z0-9]{4,8}$',
                                        message='Between 4 and 8 uppercase letters or digits')


class ChargeManager(models.Manager):
    def uninvoiced_with_total(self, account_id: str) -> Tuple[List, Total]:
        uc = Charge.objects.filter(invoice=None, account_id=account_id)
        return list(uc), total_amount(uc)

    def uninvoiced_in_currency(self, account_id: str, currency: str) -> QuerySet:
        return Charge.objects.filter(invoice=None, account_id=account_id, amount_currency=currency)


class Charge(Model):
    """
    A charge has a signed amount. If the amount is negative then the charge is in fact a credit.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    account = models.ForeignKey(Account, on_delete=PROTECT, related_name='charges')
    invoice = models.ForeignKey(Invoice, null=True, blank=True, related_name='items', on_delete=PROTECT)
    amount = MoneyField(max_digits=12, decimal_places=2)
    ad_hoc_label = models.TextField(blank=True, help_text='When not empty, this is shown verbatim to the user.')
    product_code = models.CharField(max_length=8, blank=True, validators=[product_code_validator], db_index=True,
                                    help_text='Identifies the kind of product being charged or credited')

    objects = ChargeManager()

    def clean(self):
        if not (self.ad_hoc_label or self.product_code):
            raise ValidationError('Either the ad-hoc-label or the product-code must be filled.')

    @property
    def type(self):
        a = self.amount.amount
        if a >= 0:
            return _('Charge')
        else:
            return _('Credit')

    @property
    def is_invoiced(self):
        return self.invoice is not None


product_property_name_validator = RegexValidator(regex=r'^[a-z]\w*$',
                                                 flags=re.ASCII | re.IGNORECASE,
                                                 message='a letter maybe followed by letters, numbers, or underscores')


class ProductProperty(Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    charge = models.ForeignKey(Charge, on_delete=PROTECT, related_name='product_properties')
    name = models.CharField(max_length=100, validators=[product_property_name_validator])
    value = models.CharField(max_length=255)

    class Meta:
        unique_together = ['charge', 'name']


########################################################################################################


class OnlySuccessfulTransactionsManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(success=True)


class Transaction(Model):
    """
    A transaction has a signed amount. If the amount is positive then it's a payment,
    otherwise it's a refund.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    account = models.ForeignKey(Account, related_name='transactions', on_delete=PROTECT)
    success = models.BooleanField()
    invoice = models.ForeignKey(Invoice, related_name='transactions', null=True, blank=True, on_delete=PROTECT)
    amount = MoneyField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(db_index=True, max_length=3)
    credit_card_number = models.CharField(max_length=255, blank=True)

    psp_content_type = models.ForeignKey(ContentType, on_delete=CASCADE)
    psp_object_id = models.UUIDField(db_index=True)
    psp_object = GenericForeignKey('psp_content_type', 'psp_object_id')

    objects = models.Manager()
    successful = OnlySuccessfulTransactionsManager()

    @property
    def type(self):
        a = self.amount.amount
        if a > 0:
            return _('Payment')
        elif a < 0:
            return _('Refund')

    def __str__(self):
        return '{}-{} ({})'.format(
            self.type,
            self.credit_card_number,
            'success' if self.success else 'failure')


########################################################################################################


def compute_expiry_date(two_digit_year: int, month: int) -> date:
    year = 2000 + two_digit_year
    _, last_day_of_month = calendar.monthrange(year, month)
    return date(year=year, month=month, day=last_day_of_month)


class CreditCardQuerySet(models.QuerySet):
    def valid(self, as_of: date = None):
        if as_of is None:
            as_of = datetime.now().date()
        return self.filter(expiry_date__gte=as_of)


class CreditCard(Model):
    ACTIVE = 'ACTIVE'
    INACTIVE = 'INACTIVE'
    STATUS_CHOICES = (
        (ACTIVE, _('Active')),
        (INACTIVE, _('Inactive')),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    account = models.ForeignKey(Account, related_name='credit_cards', on_delete=PROTECT)
    type = models.CharField(db_index=True, max_length=3)
    number = models.CharField(max_length=255)
    expiry_month = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    expiry_year = models.IntegerField(validators=[MinValueValidator(0), MaxValueValidator(99)])
    expiry_date = models.DateField()  # A field in the database so we can search for expired cards

    psp_content_type = models.ForeignKey(ContentType, on_delete=CASCADE)
    psp_object_id = models.UUIDField(db_index=True)
    psp_object = GenericForeignKey('psp_content_type', 'psp_object_id')

    status = FSMField(max_length=20, choices=STATUS_CHOICES, default=ACTIVE, db_index=True)

    objects = CreditCardQuerySet.as_manager()

    @transition(field=status, source=ACTIVE, target=INACTIVE)
    def deactivate(self):
        pass

    @transition(field=status, source=INACTIVE, target=ACTIVE)
    def reactivate(self):
        pass

    def is_valid(self, as_of: date = None):
        if as_of is None:
            as_of = datetime.now().date()
        return self.expiry_date >= as_of

    def save(self, *args, **kwargs):
        if self.expiry_year is not None and self.expiry_month is not None:
            self.expiry_date = compute_expiry_date(two_digit_year=self.expiry_year, month=self.expiry_month)
        super().save(*args, **kwargs)
