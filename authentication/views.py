from django.contrib.auth import authenticate, login, logout as auth_logout, update_session_auth_hash
from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth import get_user_model
from django.db.models import Q, Count
import json
import logging

from pages.models import MonitoredPage

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def login_view(request):
    """Authenticate against Django's built-in User model.

    Expected JSON:
      {"username": "user@example.com", "password": "..."}

    We treat the incoming "username" as an email for convenience.
    """
    try:
        data = json.loads(request.body)
        login_field = (data.get('username') or '').strip()
        password = data.get('password') or ''

        if not login_field or not password:
            return JsonResponse({'error': 'Username or email and password are required'}, status=400)

        # First try direct username authentication
        user = authenticate(request, username=login_field, password=password)
        if user is None:
            # Fallback to email lookup
            try:
                user_by_email = User.objects.get(email__iexact=login_field)
                user = authenticate(request, username=user_by_email.username, password=password)
            except User.DoesNotExist:
                pass  # user remains None

        if user is None:
            return JsonResponse({'error': 'Invalid username or email or password'}, status=401)

        if not user.is_active:
            return JsonResponse({'error': 'This account is disabled'}, status=403)

        # Establish a Django session (cookie-based).
        login(request, user)

        role = 'admin' if (user.is_staff or user.is_superuser) else 'user'
        full_name = (user.get_full_name() or '').strip()

        return JsonResponse(
            {
                'message': 'Login successful',
                'user': {
                    'id': str(user.id),
                    'email': user.email or '',
                    'full_name': full_name,
                    'role': role,
                },
            },
            status=200,
        )

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)
    except Exception as e:
        logger.exception("Login failed")
        return JsonResponse({'error': 'An error occurred during login', 'details': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def register_view(request):
    """Register a new Django user in auth_user.

    Expected JSON:
      {"username": "display name", "email": "user@example.com", "password": "..."}

    New users are created as normal users (non-staff).
    """
    try:
        data = json.loads(request.body)
        display_name = (data.get('username') or '').strip()
        email = (data.get('email') or '').strip()
        password = data.get('password') or ''

        if not display_name or not email or not password:
            return JsonResponse({'error': 'Username, email, and password are required'}, status=400)

        if User.objects.filter(email__iexact=email).exists():
            return JsonResponse({'error': 'An account with this email already exists'}, status=400)

        # Use the provided username exactly as the account username.
        # If it's already taken, return a clear error instead of silently altering it.
        if User.objects.filter(username__iexact=display_name).exists():
            return JsonResponse({'error': 'This username is already taken'}, status=400)

        user = User.objects.create_user(
            username=display_name,
            email=email,
            password=password,
            first_name=display_name,
            last_name='',
        )
        # Ensure normal user by default.
        user.is_staff = False
        user.is_superuser = False
        user.save(update_fields=['is_staff', 'is_superuser'])

        return JsonResponse(
            {
                'message': 'Registration successful',
                'user': {
                    'id': str(user.id),
                    'email': user.email,
                    'full_name': (user.get_full_name() or '').strip(),
                    'role': 'user',
                },
            },
            status=201,
        )

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)
    except Exception as e:
        logger.exception("Registration failed")
        return JsonResponse({'error': 'An error occurred during registration', 'details': str(e)}, status=500)


@require_http_methods(["GET"])
def me_view(request):
    """Return the currently authenticated user (session-based)."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    role = 'admin' if (user.is_staff or user.is_superuser) else 'user'
    return JsonResponse(
        {
            'user': {
                'id': str(user.id),
                'email': user.email or '',
                'username': user.username,
                'full_name': (user.get_full_name() or '').strip(),
                'role': role,
                'date_joined': user.date_joined.isoformat(),
            }
        },
        status=200,
    )


@csrf_exempt
@require_http_methods(["PATCH"])
def update_profile_view(request):
    """Update username and/or email for the authenticated user."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    changed = []

    new_username = (data.get('username') or '').strip()
    if new_username and new_username != user.username:
        if User.objects.filter(username__iexact=new_username).exclude(pk=user.pk).exists():
            return JsonResponse({'error': 'Username already taken'}, status=400)
        user.username = new_username
        user.first_name = new_username
        changed.append('username')
        changed.append('first_name')

    new_email = (data.get('email') or '').strip()
    if new_email and new_email != user.email:
        if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
            return JsonResponse({'error': 'Email already in use'}, status=400)
        user.email = new_email
        changed.append('email')

    if changed:
        user.save(update_fields=changed)

    role = 'admin' if (user.is_staff or user.is_superuser) else 'user'
    return JsonResponse({
        'user': {
            'id': str(user.id),
            'email': user.email or '',
            'username': user.username,
            'full_name': (user.get_full_name() or '').strip(),
            'role': role,
        }
    }, status=200)


@csrf_exempt
@require_http_methods(["POST"])
def change_password_view(request):
    """Change password for the authenticated user."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    current_password = data.get('current_password') or ''
    new_password = (data.get('new_password') or '').strip()

    if not current_password or not new_password:
        return JsonResponse({'error': 'current_password and new_password are required'}, status=400)

    if len(new_password) < 8:
        return JsonResponse({'error': 'New password must be at least 8 characters'}, status=400)

    if not user.check_password(current_password):
        return JsonResponse({'error': 'Current password is incorrect'}, status=400)

    user.set_password(new_password)
    user.save()
    # Keep the session alive after password change
    update_session_auth_hash(request, user)

    return JsonResponse({'message': 'Password changed successfully'}, status=200)


@csrf_exempt
@require_http_methods(["DELETE"])
def delete_account_view(request):
    """Permanently delete the authenticated user's account and all their data."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    password = data.get('password') or ''
    if not password:
        return JsonResponse({'error': 'Password is required to delete account'}, status=400)

    if not user.check_password(password):
        return JsonResponse({'error': 'Incorrect password'}, status=400)

    # Delete all screenshot artefacts for this user's pages via storage abstraction
    from pages.screenshots import delete_screenshot_file, _thumb_rel
    for page in MonitoredPage.objects.filter(user=user):
        for check in page.checks.all():
            delete_screenshot_file(check.screenshot_path)
            delete_screenshot_file(check.crop_path)
            delete_screenshot_file(check.diff_path)
            if check.screenshot_path:
                delete_screenshot_file(_thumb_rel(check.screenshot_path))

    auth_logout(request)
    user.delete()
    return JsonResponse({'message': 'Account deleted'}, status=200)


def _ensure_admin(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)
    if not (user.is_staff or user.is_superuser):
        return JsonResponse({'error': 'Admin access required'}, status=403)
    return None


@require_http_methods(["GET"])
def admin_user_search(request):
    """Search users by username or email (admin-only)."""
    denial = _ensure_admin(request)
    if denial is not None:
        return denial

    query = (request.GET.get('query') or '').strip()
    if not query:
        return JsonResponse({'users': []}, status=200)

    User = get_user_model()
    users = (
        User.objects.filter(Q(username__icontains=query) | Q(email__icontains=query))
        .annotate(monitored_sites_count=Count('monitored_pages'))
        .order_by('username')
    )

    results = []
    for user in users:
        results.append(
            {
                'id': str(user.id),
                'username': user.username,
                'email': user.email or '',
                'full_name': (user.get_full_name() or '').strip(),
                'monitored_sites_count': user.monitored_sites_count,
                'is_active': user.is_active,
                'is_staff': user.is_staff,
                'date_joined': user.date_joined.isoformat(),
            }
        )

    return JsonResponse({'users': results}, status=200)


@require_http_methods(["GET"])
def admin_user_sites(request, user_id):
    """List monitored sites for a user (admin-only)."""
    denial = _ensure_admin(request)
    if denial is not None:
        return denial

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)

    pages = MonitoredPage.objects.filter(user=user).order_by('-created_at')
    results = [
        {
            'id': str(page.id),
            'url': page.url,
            'created_at': page.created_at.isoformat(),
        }
        for page in pages
    ]

    return JsonResponse({'sites': results}, status=200)
