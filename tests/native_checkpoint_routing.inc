/* Test-only hooks, appended after routing.inc. */
void es_test_poison_routing(void)
{
    NextEvent = 0;
    BetweenEvents = (NumEvents > 0);
    NewRuleTime = 0;
}
