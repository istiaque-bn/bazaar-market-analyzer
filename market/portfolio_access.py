"""Shared portfolio ownership boundary for browser views.

The helper is re-exported by ``market.views`` to preserve the established
internal import path while keeping the authorization rule close to portfolio
code rather than the general view facade.
"""
from django.shortcuts import get_object_or_404

from market.models import Portfolio


def owned_portfolio(request, portfolio_id):
    """Return a portfolio only when it belongs to the requesting user."""
    return get_object_or_404(Portfolio, id=portfolio_id, user=request.user)
