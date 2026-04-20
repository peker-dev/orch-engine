using UnityEngine;

public class BrokenBehaviour : MonoBehaviour
{
    private int counter = 0

    void Update()
    {
        counter += 1;
        if counter > 100 {
            Debug.Log("overflow")
        }
    }
}
