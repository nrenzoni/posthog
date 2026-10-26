import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { router } from 'kea-router'

import { initKeaTests } from '~/test/init'

import { SSOEnforcedLoginButton } from './SocialLoginButton'
import { submitSocialLogin } from './socialLoginUrl'

describe('social login POST navigation', () => {
    let submittedForm: HTMLFormElement
    let submitMock: jest.SpyInstance

    beforeEach(() => {
        initKeaTests()
        document.cookie = 'posthog_csrftoken=social-login-csrf'
        submitMock = jest
            .spyOn(HTMLFormElement.prototype, 'submit')
            .mockImplementation(function (this: HTMLFormElement) {
                submittedForm = this
            })
    })

    afterEach(() => {
        cleanup()
        jest.restoreAllMocks()
        document.cookie = 'posthog_csrftoken=; Max-Age=0'
    })

    it('POSTs from the actual provider button with email, invite and reactive next hints', () => {
        router.actions.push('/login?next=%2Fproject%2F1%3Ftab%3Dinsights')
        render(
            <SSOEnforcedLoginButton
                provider="google-oauth2"
                email="member@example.com"
                extraQueryParams={{ invite_id: 'invite-123' }}
            />
        )

        const button = screen.getByRole('button', { name: /Log in with Google/ })
        expect(button.closest('a')).toBeNull() // No alternate-click GET initiation path
        fireEvent.click(button)

        expect(submitMock).toHaveBeenCalledTimes(1)
        expect(submittedForm.method).toBe('post')
        expect(submittedForm.target).toBe('_self')
        const action = new URL(submittedForm.action)
        expect(action.origin).toBe(location.origin)
        expect(action.pathname).toBe('/login/google-oauth2/')
        expect(action.searchParams.get('email')).toBe('member@example.com')
        expect(action.searchParams.get('invite_id')).toBe('invite-123')
        expect(action.searchParams.get('next')).toBe('/project/1?tab=insights')
        expect(Array.from(submittedForm.elements)).toEqual([
            expect.objectContaining({
                tagName: 'INPUT',
                type: 'hidden',
                name: 'csrfmiddlewaretoken',
                value: 'social-login-csrf',
                disabled: false,
            }),
        ])
        expect(document.body.contains(submittedForm)).toBe(false)
    })

    it('delegates a reauthentication button to its caller without submitting twice', () => {
        const onProviderClick = jest.fn()
        render(
            <SSOEnforcedLoginButton
                provider="google-oauth2"
                email="member@example.com"
                onProviderClick={onProviderClick}
            />
        )
        fireEvent.click(screen.getByRole('button', { name: /Log in with Google/ }))
        expect(onProviderClick).toHaveBeenCalledWith('google-oauth2')
        expect(submitMock).not.toHaveBeenCalled()
    })

    it('preserves SAML routing, reauth/attempt and connection hints without putting them in the body', () => {
        const params = {
            email: 'member@example.com',
            reauth: 'true',
            next: '/reauth/complete?attempt=attempt-123',
            connect_from: 'posthog_code',
        }
        submitSocialLogin('saml', params, {}, '_blank')
        const action = new URL(submittedForm.action)
        for (const [name, value] of Object.entries(params)) {
            expect(action.searchParams.get(name)).toBe(value)
        }
        expect(action.searchParams.get('idp')).toBe('posthog_custom')
        expect(submittedForm.method).toBe('post')
        expect(submittedForm.target).toBe('_blank')
        expect(submittedForm.rel).toBe('noopener')
        expect(Array.from(submittedForm.elements)).toEqual([
            expect.objectContaining({
                tagName: 'INPUT',
                type: 'hidden',
                name: 'csrfmiddlewaretoken',
                value: 'social-login-csrf',
                disabled: false,
            }),
        ])
    })
})
