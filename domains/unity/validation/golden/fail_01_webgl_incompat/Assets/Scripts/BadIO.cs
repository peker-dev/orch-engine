using System.IO;
using UnityEngine;

public class BadIO : MonoBehaviour
{
    private void Start()
    {
        File.WriteAllText("log.txt", "not allowed on webgl");
    }
}
