class PullRequestWait:
    def pull_request_conflicted(self, number: int) -> None:
        """Signal 'pull_request_conflicted', arg [number]: GitHub reported (a pull_request
        webhook event) that pull request `number` may conflict with its base branch. A
        waiting delivery or onboarding whose pull request it is runs the activity
        'pull_request_conflicts' at once instead of at the next check interval; a signal
        about another pull request, or while no pull request exists, changes nothing."""
        ...
