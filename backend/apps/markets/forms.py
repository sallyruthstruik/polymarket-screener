from __future__ import annotations

import json

from django import forms

BOOL_CHOICES = (
    ("", "Unknown"),
    ("true", "Yes"),
    ("false", "No"),
)


class NullableBooleanChoiceField(forms.TypedChoiceField):
    def __init__(self, *, required: bool = False, label: str) -> None:
        super().__init__(
            label=label,
            required=required,
            choices=BOOL_CHOICES,
            coerce=self._coerce_value,
            empty_value=None,
        )

    def _coerce_value(self, value: str) -> bool | None:
        if value == "true":
            return True
        if value == "false":
            return False
        return None


class PolymarketMarketAdminForm(forms.Form):
    condition_id = forms.CharField(max_length=128, required=False)
    slug = forms.CharField(max_length=512, required=False)
    question = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 5}))
    category = forms.CharField(max_length=255, required=False)

    active = NullableBooleanChoiceField(label="Active")
    closed = NullableBooleanChoiceField(label="Closed")
    archived = NullableBooleanChoiceField(label="Archived")
    restricted = NullableBooleanChoiceField(label="Restricted")
    accepting_orders = NullableBooleanChoiceField(label="Accepting orders")

    market_created_at = forms.DateTimeField(required=False)
    market_updated_at = forms.DateTimeField(required=False)
    start_date = forms.DateTimeField(required=False)
    end_date = forms.DateTimeField(required=False)

    liquidity = forms.DecimalField(max_digits=38, decimal_places=12, required=False)
    volume = forms.DecimalField(max_digits=38, decimal_places=12, required=False)
    liquidity_clob = forms.DecimalField(max_digits=38, decimal_places=12, required=False)
    volume_clob = forms.DecimalField(max_digits=38, decimal_places=12, required=False)
    volume_24hr = forms.DecimalField(max_digits=38, decimal_places=12, required=False)

    clob_token_ids = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    sync_prices = forms.BooleanField(required=False)

    def clean_clob_token_ids(self) -> list[str]:
        raw_value = self.cleaned_data["clob_token_ids"]
        if raw_value == "":
            return []
        parsed: object = json.loads(raw_value)
        if not isinstance(parsed, list):
            msg = "clob_token_ids must be a JSON array."
            raise forms.ValidationError(msg)
        token_ids: list[str] = []
        for item in parsed:
            if not isinstance(item, str):
                msg = "clob_token_ids must contain only strings."
                raise forms.ValidationError(msg)
            token_ids.append(item)
        return token_ids
