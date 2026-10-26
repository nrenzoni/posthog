import { combineUrl, router } from 'kea-router'

import { getCookie } from 'lib/api'

import { SSOProvider } from '~/types'

/**
 * Build the Django SSO initiation action URL, shared by buttons and kea logics.
 * Navigate with submitSocialLogin, never a GET link.
 */
export function getSocialLoginUrl(
    provider: SSOProvider,
    extraQueryParams?: Record<string, string>,
    // Components pass their subscribed `searchParams` so the destination stays reactive.
    searchParams: Record<string, any> = router.values.searchParams
): string {
    const loginParams: Record<string, string> = { ...extraQueryParams }
    const { next } = searchParams
    if (next) {
        loginParams.next = next
    }
    if (provider === 'saml') {
        // SAML-based login requires an extra param as technically we can support multiple SAML backends
        loginParams.idp = 'posthog_custom'
    }
    return combineUrl(`/login/${provider}/`, loginParams).url
}

/**
 * Initiation is a same-origin, CSRF-protected POST navigation, not an API request.
 * Keep routing hints in the action query: the Django SSO entrypoint reads request.GET,
 * and social-auth persists them with its generated state in the initiating session.
 */
export function submitSocialLogin(
    provider: SSOProvider,
    extraQueryParams?: Record<string, string>,
    searchParams: Record<string, any> = router.values.searchParams,
    target = '_self'
): void {
    const action = new URL(getSocialLoginUrl(provider, extraQueryParams, searchParams), window.location.origin)
    if (action.origin !== window.location.origin) {
        throw new Error('SSO initiation must be same-origin')
    }

    const form = document.createElement('form')
    form.method = 'POST'
    form.action = action.href
    form.target = target
    form.style.display = 'none'
    if (target === '_blank') {
        form.rel = 'noopener'
    }

    const csrfInput = document.createElement('input')
    csrfInput.type = 'hidden'
    csrfInput.name = 'csrfmiddlewaretoken'
    csrfInput.value = getCookie('posthog_csrftoken') || ''
    form.appendChild(csrfInput)
    document.body.appendChild(form)
    try {
        form.submit()
    } finally {
        form.remove()
    }
}
